# Copyright 2019-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Client side encryption."""

import functools
import subprocess
import uuid
import weakref

try:
    from pymongocrypt.auto_encrypter import AutoEncrypter
    from pymongocrypt.errors import MongoCryptError
    from pymongocrypt.explicit_encrypter import ExplicitEncrypter
    from pymongocrypt.mongocrypt import MongoCryptOptions
    from pymongocrypt.state_machine import MongoCryptCallback
    _HAVE_PYMONGOCRYPT = True
except ImportError:
    _HAVE_PYMONGOCRYPT = False
    MongoCryptCallback = object

from bson import _bson_to_dict, _dict_to_bson, decode, encode
from bson.codec_options import CodecOptions
from bson.binary import STANDARD, Binary
from bson.errors import BSONError
from bson.raw_bson import (DEFAULT_RAW_BSON_OPTIONS,
                           RawBSONDocument,
                           _inflate_bson)
from bson.son import SON

from pymongo.errors import (ConfigurationError,
                            EncryptionError,
                            ServerSelectionTimeoutError)
from pymongo.mongo_client import MongoClient
from pymongo.pool import _configured_socket, PoolOptions
from pymongo.ssl_support import get_ssl_context


_HTTPS_PORT = 443
_KMS_CONNECT_TIMEOUT = 10  # TODO: CDRIVER-3262 will define this value.
_MONGOCRYPTD_TIMEOUT_MS = 1000

_DATA_KEY_OPTS = CodecOptions(document_class=SON, uuid_representation=STANDARD)
# Use RawBSONDocument codec options to avoid needlessly decoding
# documents from the key vault.
_KEY_VAULT_OPTS = CodecOptions(document_class=RawBSONDocument,
                               uuid_representation=STANDARD)


def _wrap_encryption_errors(encryption_func=None):
    """Decorator to wrap encryption related errors with EncryptionError."""
    @functools.wraps(encryption_func)
    def wrap_encryption_errors(*args, **kwargs):
        try:
            return encryption_func(*args, **kwargs)
        except BSONError:
            # BSON encoding/decoding errors are unrelated to encryption so
            # we should propagate them unchanged.
            raise
        except Exception as exc:
            raise EncryptionError(exc)

    return wrap_encryption_errors


class _EncryptionIO(MongoCryptCallback):
    def __init__(self, client, key_vault_coll, mongocryptd_client, opts):
        """Internal class to perform I/O on behalf of pymongocrypt."""
        # Use a weak ref to break reference cycle.
        if client is not None:
            self.client_ref = weakref.ref(client)
        else:
            self.client_ref = None
        self.key_vault_coll = key_vault_coll.with_options(
            codec_options=_KEY_VAULT_OPTS)
        self.mongocryptd_client = mongocryptd_client
        self.opts = opts
        self._spawned = False

    def kms_request(self, kms_context):
        """Complete a KMS request.

        :Parameters:
          - `kms_context`: A :class:`MongoCryptKmsContext`.

        :Returns:
          None
        """
        endpoint = kms_context.endpoint
        message = kms_context.message
        ctx = get_ssl_context(None, None, None, None, None, None, True)
        opts = PoolOptions(connect_timeout=_KMS_CONNECT_TIMEOUT,
                           socket_timeout=_KMS_CONNECT_TIMEOUT,
                           ssl_context=ctx)
        with _configured_socket((endpoint, _HTTPS_PORT), opts) as conn:
            conn.sendall(message)
            while kms_context.bytes_needed > 0:
                data = conn.recv(kms_context.bytes_needed)
                kms_context.feed(data)

    def collection_info(self, database, filter):
        """Get the collection info for a namespace.

        The returned collection info is passed to libmongocrypt which reads
        the JSON schema.

        :Parameters:
          - `database`: The database on which to run listCollections.
          - `filter`: The filter to pass to listCollections.

        :Returns:
          The first document from the listCollections command response as BSON.
        """
        with self.client_ref()[database].list_collections(
                filter=RawBSONDocument(filter)) as cursor:
            for doc in cursor:
                return _dict_to_bson(doc, False, _DATA_KEY_OPTS)

    def spawn(self):
        """Spawn mongocryptd.

        Note this method is thread safe; at most one mongocryptd will start
        successfully.
        """
        self._spawned = True
        args = [self.opts._mongocryptd_spawn_path or 'mongocryptd']
        args.extend(self.opts._mongocryptd_spawn_args)
        subprocess.Popen(args)

    def mark_command(self, database, cmd):
        """Mark a command for encryption.

        :Parameters:
          - `database`: The database on which to run this command.
          - `cmd`: The BSON command to run.

        :Returns:
          The marked command response from mongocryptd.
        """
        if not self._spawned and not self.opts._mongocryptd_bypass_spawn:
            self.spawn()
        # Database.command only supports mutable mappings so we need to decode
        # the raw BSON command first.
        inflated_cmd = _inflate_bson(cmd, DEFAULT_RAW_BSON_OPTIONS)
        try:
            res = self.mongocryptd_client[database].command(
                inflated_cmd,
                codec_options=DEFAULT_RAW_BSON_OPTIONS)
        except ServerSelectionTimeoutError:
            if self.opts._mongocryptd_bypass_spawn:
                raise
            self.spawn()
            res = self.mongocryptd_client[database].command(
                inflated_cmd,
                codec_options=DEFAULT_RAW_BSON_OPTIONS)
        return res.raw

    def fetch_keys(self, filter):
        """Yields one or more keys from the key vault.

        :Parameters:
          - `filter`: The filter to pass to find.

        :Returns:
          A generator which yields the requested keys from the key vault.
        """
        with self.key_vault_coll.find(RawBSONDocument(filter)) as cursor:
            for key in cursor:
                yield key.raw

    def insert_data_key(self, data_key):
        """Insert a data key into the key vault.

        :Parameters:
          - `data_key`: The data key document to insert.

        :Returns:
          The _id of the inserted data key document.
        """
        # insert does not return the inserted _id when given a RawBSONDocument.
        doc = _bson_to_dict(data_key, _DATA_KEY_OPTS)
        res = self.key_vault_coll.insert_one(doc)
        return res.inserted_id

    def bson_encode(self, doc):
        """Encode a document to BSON.

        A document can be any mapping type (like :class:`dict`).

        :Parameters:
          - `doc`: mapping type representing a document

        :Returns:
          The encoded BSON bytes.
        """
        return encode(doc)

    def close(self):
        """Release resources.

        Note it is not safe to call this method from __del__ or any GC hooks.
        """
        self.client_ref = None
        self.key_vault_coll = None
        if self.mongocryptd_client:
            self.mongocryptd_client.close()
            self.mongocryptd_client = None


class _Encrypter(object):
    def __init__(self, io_callbacks, opts):
        """Encrypts and decrypts MongoDB commands.

        This class is used to support automatic encryption and decryption of
        MongoDB commands.

        :Parameters:
          - `io_callbacks`: A :class:`MongoCryptCallback`.
          - `opts`: The encrypted client's :class:`AutoEncryptionOpts`.
        """
        if opts._schema_map is None:
            schema_map = None
        else:
            schema_map = _dict_to_bson(opts._schema_map, False, _DATA_KEY_OPTS)
        self._auto_encrypter = AutoEncrypter(io_callbacks, MongoCryptOptions(
            opts._kms_providers, schema_map))
        self._bypass_auto_encryption = opts._bypass_auto_encryption

    @_wrap_encryption_errors
    def encrypt(self, database, cmd, check_keys, codec_options):
        """Encrypt a MongoDB command.

        :Parameters:
          - `database`: The database for this command.
          - `cmd`: A command document.
          - `check_keys`: If True, check `cmd` for invalid keys.
          - `codec_options`: The CodecOptions to use while encoding `cmd`.

        :Returns:
          The encrypted command to execute.
        """
        # Workaround for $clusterTime which is incompatible with check_keys.
        cluster_time = check_keys and cmd.pop('$clusterTime', None)
        encoded_cmd = _dict_to_bson(cmd, check_keys, codec_options)
        encrypted_cmd = self._auto_encrypter.encrypt(database, encoded_cmd)
        # TODO: PYTHON-1922 avoid decoding the encrypted_cmd.
        encrypt_cmd = _inflate_bson(encrypted_cmd, DEFAULT_RAW_BSON_OPTIONS)
        if cluster_time:
            encrypt_cmd['$clusterTime'] = cluster_time
        return encrypt_cmd

    @_wrap_encryption_errors
    def decrypt(self, response):
        """Decrypt a MongoDB command response.

        :Parameters:
          - `response`: A MongoDB command response as BSON.

        :Returns:
          The decrypted command response.
        """
        return self._auto_encrypter.decrypt(response)

    def close(self):
        """Cleanup resources."""
        self._auto_encrypter.close()

    @staticmethod
    def create(client, opts):
        """Create a _CommandEncyptor for a client.

        :Parameters:
          - `client`: The encrypted MongoClient.
          - `opts`: The encrypted client's :class:`AutoEncryptionOpts`.

        :Returns:
          A :class:`_CommandEncrypter` for this client.
        """
        key_vault_client = opts._key_vault_client or client
        db, coll = opts._key_vault_namespace.split('.', 1)
        key_vault_coll = key_vault_client[db][coll]

        mongocryptd_client = MongoClient(
            opts._mongocryptd_uri, connect=False,
            serverSelectionTimeoutMS=_MONGOCRYPTD_TIMEOUT_MS)

        io_callbacks = _EncryptionIO(
            client, key_vault_coll, mongocryptd_client, opts)
        return _Encrypter(io_callbacks, opts)


class Algorithm(object):
    """An enum that defines the supported encryption algorithms."""
    Deterministic = "AEAD_AES_256_CBC_HMAC_SHA_512-Deterministic"
    Random = "AEAD_AES_256_CBC_HMAC_SHA_512-Random"


class ClientEncryption(object):
    """Explicit client side encryption."""

    def __init__(self, kms_providers, key_vault_namespace, key_vault_client,
                 codec_options):
        """Explicit client side encryption.

        The ClientEncryption class encapsulates explicit operations on a key
        vault collection that cannot be done directly on a MongoClient. Similar
        to configuring auto encryption on a MongoClient, it is constructed with
        a MongoClient (to a MongoDB cluster containing the key vault
        collection), KMS provider configuration, and keyVaultNamespace. It
        provides an API for explicitly encrypting and decrypting values, and
        creating data keys. It does not provide an API to query keys from the
        key vault collection, as this can be done directly on the MongoClient.

        :Parameters:
          - `kms_providers`: Map of KMS provider options. Two KMS providers
            are supported: "aws" and "local". The kmsProviders map values
            differ by provider:

              - `aws`: Map with "accessKeyId" and "secretAccessKey" as strings.
                These are the AWS access key ID and AWS secret access key used
                to generate KMS messages.
              - `local`: Map with "key" as a 96-byte array or string. "key"
                is the master key used to encrypt/decrypt data keys. This key
                should be generated and stored as securely as possible.

          - `key_vault_namespace`: The namespace for the key vault collection.
            The key vault collection contains all data keys used for encryption
            and decryption. Data keys are stored as documents in this MongoDB
            collection. Data keys are protected with encryption by a KMS
            provider.
          - `key_vault_client`: A MongoClient connected to a MongoDB cluster
            containing the `key_vault_namespace` collection.
          - `codec_options`: An instance of
            :class:`~bson.codec_options.CodecOptions` to use when encoding a
            value for encryption and decoding the decrypted BSON value.

        .. versionadded:: 3.9
        """
        if not _HAVE_PYMONGOCRYPT:
            raise ConfigurationError(
                "client side encryption requires the pymongocrypt library: "
                "install a compatible version with: "
                "python -m pip install pymongo['encryption']")

        if not isinstance(codec_options, CodecOptions):
            raise TypeError("codec_options must be an instance of "
                            "bson.codec_options.CodecOptions")

        self._kms_providers = kms_providers
        self._key_vault_namespace = key_vault_namespace
        self._key_vault_client = key_vault_client
        self._codec_options = codec_options

        db, coll = key_vault_namespace.split('.', 1)
        key_vault_coll = key_vault_client[db][coll]

        self._io_callbacks = _EncryptionIO(None, key_vault_coll, None, None)
        self._encryption = ExplicitEncrypter(
            self._io_callbacks, MongoCryptOptions(kms_providers, None))

    @_wrap_encryption_errors
    def create_data_key(self, kms_provider, master_key=None,
                        key_alt_names=None):
        """Create and insert a new data key into the key vault collection.

        :Parameters:
          - `kms_provider`: The KMS provider to use. Supported values are
            "aws" and "local".
          - `master_key`: The `master_key` identifies a KMS-specific key used
            to encrypt the new data key. If the kmsProvider is "local" the
            `master_key` is not applicable and may be omitted.
            If the `kms_provider` is "aws", `master_key` is required and must
            have the following fields:

              - `region` (string): The AWS region as a string.
              - `key` (string): The Amazon Resource Name (ARN) to the AWS
                customer master key (CMK).

          - `key_alt_names` (optional): An optional list of string alternate
            names used to reference a key. If a key is created with alternate
            names, then encryption may refer to the key by the unique alternate
            name instead of by ``key_id``. The following example shows creating
            and referring to a data key by alternate name::

              client_encryption.create_data_key("local", keyAltNames=["name1"])
              # reference the key with the alternate name
              client_encryption.encrypt("457-55-5462", keyAltName="name1",
                                        algorithm=Algorithm.Random)

        :Returns:
          The ``_id`` of the created data key document.
        """
        return self._encryption.create_data_key(
            kms_provider, master_key=master_key, key_alt_names=key_alt_names)

    @_wrap_encryption_errors
    def encrypt(self, value, algorithm, key_id=None, key_alt_name=None):
        """Encrypt a BSON value with a given key and algorithm.

        Note that exactly one of ``key_id`` or  ``key_alt_name`` must be
        provided.

        :Parameters:
          - `value`: The BSON value to encrypt.
          - `algorithm` (string): The encryption algorithm to use. See
            :class:`Algorithm` for some valid options.
          - `key_id`: Identifies a data key by ``_id`` which must be a UUID
            or a :class:`~bson.binary.Binary` with subtype 4.
          - `key_alt_name`: Identifies a key vault document by 'keyAltName'.

        :Returns:
          The encrypted value, a :class:`~bson.binary.Binary` with subtype 6.
        """
        doc = encode({'v': value}, codec_options=self._codec_options)
        if isinstance(key_id, uuid.UUID):
            raw_key_id = key_id.bytes
        else:
            raw_key_id = key_id
        encrypted_doc = self._encryption.encrypt(
            doc, algorithm, key_id=raw_key_id, key_alt_name=key_alt_name)
        return decode(encrypted_doc)['v']

    @_wrap_encryption_errors
    def _decrypt(self, value):
        """Internal decrypt helper."""
        doc = encode({'v': value})
        decrypted_doc = self._encryption.decrypt(doc)
        return decode(decrypted_doc, codec_options=self._codec_options)['v']

    def decrypt(self, value):
        """Decrypt an encrypted value.

        :Parameters:
          - `value` (Binary): The encrypted value, a
            :class:`~bson.binary.Binary` with subtype 6.

        :Returns:
          The decrypted BSON value.
        """
        if not (isinstance(value, Binary) and value.subtype == 6):
            raise TypeError(
                'value to decrypt must be a bson.binary.Binary with subtype 6')

        return self._decrypt(value)

    def close(self):
        """Release resources."""
        self._io_callbacks.close()
        self._encryption.close()
        self._io_callbacks = None
        self._encryption = None