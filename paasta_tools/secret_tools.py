# Copyright 2015-2017 Yelp Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import os
import re
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from paasta_tools.secret_providers import SecretProvider

SECRET_REGEX = "^SECRET\([A-Za-z0-9_-]*\)$"


def is_secret_ref(env_var_val: str) -> bool:
    pattern = re.compile(SECRET_REGEX)
    return pattern.match(env_var_val) is not None


def get_hmac_for_secret(
    env_var_val: str,
    service: str,
    soa_dir: str,
    vault_environment: str,
) -> Optional[str]:
    secret_name = get_secret_name_from_ref(env_var_val)
    secret_path = os.path.join(
        soa_dir,
        service,
        "secrets", "{}.json".format(secret_name),
    )
    try:
        with open(secret_path, 'r') as json_secret_file:
            secret_file = json.load(json_secret_file)
            try:
                return secret_file['environments'][vault_environment]['signature']
            except KeyError:
                print("Failed to get secret signature at environments:{}:signature in json"
                      " file".format(vault_environment))
                return None
    except IOError as e:
        print("Failed to open json secret at {}".format(secret_path))
        return None
    except json.decoder.JSONDecodeError as e:
        print("Failed to deserialise json secret at {}".format(secret_path))
        return None


def get_secret_name_from_ref(env_var_val: str) -> str:
    return env_var_val.split('(')[1][:-1]


def get_secret_provider(
    secret_provider_name: str,
    soa_dir: str,
    service_name: str,
    cluster_names: List[str],
    secret_provider_kwargs: Dict[str, Any],
) -> SecretProvider:
    SecretProvider = __import__(secret_provider_name, fromlist=['SecretProvider']).SecretProvider
    return SecretProvider(
        soa_dir=soa_dir,
        service_name=service_name,
        cluster_names=cluster_names,
        **secret_provider_kwargs,
    )
