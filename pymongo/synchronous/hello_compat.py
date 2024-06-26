# Copyright 2024-present MongoDB, Inc.
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

"""The HelloCompat class, placed here to break circular import issues."""
from __future__ import annotations

_IS_SYNC = True


class HelloCompat:
    CMD = "hello"
    LEGACY_CMD = "ismaster"
    PRIMARY = "isWritablePrimary"
    LEGACY_PRIMARY = "ismaster"
    LEGACY_ERROR = "not master"
