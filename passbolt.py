import httpx
from pprint import pprint
import json
from urllib.parse import unquote
import gnupg
import tempfile
import shutil
from pathlib import Path
import os

# https://gnupg.readthedocs.io/en/latest/
# https://gist.github.com/ryantuck/56c5aaa8f9124422ac964629f4c8deb0


class PassboltAPI:
    def __init__(
        self,
        config_filename="config.json",
        config_filepath=Path(__file__).parent.resolve(),
        dict_config=dict(),
    ):

        self.load_config(
            config_filename=config_filename,
            config_filepath=config_filepath,
            dict_config=dict_config,
        )

        self.gnupghome = tempfile.mkdtemp()
        self.gpg = gnupg.GPG(
            gpgbinary=self.config.get("gpgbinary", "gpg"), gnupghome=self.gnupghome
        )

        # Load key
        self.FINGERPRINT = self.gpg.import_keys(
            self.config.get("private_key")
        ).fingerprints[0]

        self.base_url = self.config.get("base_url")
        self.login_url = f"{self.base_url}/auth/login.json"
        self.users_url = f"{self.base_url}/users.json"
        self.me_url = f"{self.base_url}/users/me.json"
        self.groups_url = f"{self.base_url}/groups.json"

        # vars definition
        self.authenticated = False
        self.token = None
        self.USER_ID = None
        self.pgp_message = None
        self.nonce = None

        self.session = httpx.Client(verify=False)
        self.cookies = httpx.Cookies()

        self.login()

    def __del__(self):
        """
        Destroy temporary gpghome when object is destroyed
        """
        shutil.rmtree(self.gnupghome)

    def load_config(
        self,
        config_filename="config.json",
        config_filepath=Path(__file__).parent.resolve(),
        dict_config=dict(),
    ):

        if dict_config:
            self.config = dict_config
        elif Path(config_filepath, config_filename).is_file():
            with open(Path(config_filepath, config_filename)) as config_file:
                self.config = json.load(config_file)
        else:
            self.config = {
                "gpgbinary": os.environ.get("PASSBOLT_GPGBINARY", "gpg"),
                "base_url": os.environ.get("PASSBOLT_BASEURL", "https://undefined"),
                "private_key": os.environ.get("PASSBOLT_PRIVATE_KEY", "undefined"),
                "passphrase": os.environ.get("PASSBOLT_PASSPHRASE", "gpg"),
            }

    def stage1(self):
        post = {"data": {"gpg_auth": {"keyid": self.FINGERPRINT}}}

        response = self.session.post(self.login_url, json=post)
        decoded_response = json.loads(response.text)

        if decoded_response["header"]["code"] == 200:
            pgp_message = unquote(
                response.headers.get("x-gpgauth-user-auth-token")
            ).replace("\\+", " ")
            return pgp_message

        else:
            pprint(decoded_response)

    def decrypt(self, message):
        decrypt = self.gpg.decrypt(message, passphrase=self.config.get("passphrase"))
        return decrypt

    def encrypt(self, message, public_key):
        self.gpg.import_keys(public_key["armored_key"])
        encrypt = self.gpg.encrypt(
            message, public_key["fingerprint"], always_trust=True
        )
        return encrypt

    def stage2(self, nonce):
        post = {
            "data": {
                "gpg_auth": {"keyid": self.FINGERPRINT, "user_token_result": nonce}
            }
        }

        response = self.session.post(self.login_url, json=post)
        decoded_response = json.loads(response.text)
        # self.USER_ID = decoded_response['body']['id']

        if decoded_response["header"]["code"] == 200:
            return True
        else:
            return False

    def get_cookie(self):
        response = self.session.get(self.me_url)
        token = response.headers.get("set-cookie")
        user_id = json.loads(response.text)
        self.USER_ID = user_id["body"]["id"]
        self.token = token[10:-8]
        self.session.headers = {"X-CSRF-Token": self.token}

    def check_login(self):
        response = self.session.get(self.base_url + "/")
        if response.status_code != 200:
            print("Falha no login")
            print(response)

    def login(self):
        self.pgp_message = self.stage1()
        self.nonce = self.decrypt(self.pgp_message)
        self.authenticated = self.stage2(str(self.nonce))
        self.get_cookie()
        self.check_login()

    def get_users(self):
        response = self.session.get(self.users_url)
        decoded_response = json.loads(response.text)
        return decoded_response["body"]

    def get_groups(self):
        response = self.session.get(self.groups_url)
        decoded_response = json.loads(response.text)
        return decoded_response["body"]

    def get_user_by_email(self, email):
        users = self.get_users()
        for user in users:
            if user["username"] == email:
                return user

    def get_user_by_id(self, id):
        users = self.get_users()
        for user in users:
            if user["id"] == id:
                return user

    def get_group_by_name(self, group_name):
        groups = self.get_groups()
        for group in groups:
            if group["name"] == group_name:
                return group

    def create_group(self, group_name):
        post = {
            "name": group_name,
            "groups_users": [{"user_id": self.USER_ID, "is_admin": True}],
        }

        response = self.session.post(self.groups_url, json=post)

        return response

    def put_user_on_group(self, group_id, user_id, admin=False):
        post = {
            "id": group_id,
            "groups_users": [{"user_id": user_id, "is_admin": admin}],
        }
        url = f"{self.base_url}/groups/{group_id}/dry-run.json"
        response = self.session.put(url, json=post)
        if response.status_code == 200:
            user_key = self.get_user_public_key(user_id)
            secrets = json.loads(response.text)["body"]["dry-run"]["Secrets"]

            secrets_list = []
            for secret in secrets:
                decrypted = self.decrypt(secret["Secret"][0]["data"])
                reencrypted = self.encrypt(str(decrypted), user_key)

                secrets_list.append(
                    {
                        "resource_id": secret["Secret"][0]["resource_id"],
                        "user_id": user_id,
                        "data": str(reencrypted),
                    }
                )

            post = {
                "id": group_id,
                "groups_users": [{"user_id": user_id, "is_admin": admin}],
                "secrets": secrets_list,
            }

            url = f"{self.base_url}/groups/{group_id}.json"
            response = self.session.put(url, json=post)
        else:
            print(response.headers)
            print()
            print(response.text)
            print()

        return response

    def get_group_by_id(self, group_id):
        groups = self.get_groups()
        for group in groups:
            if group["id"] == group_id:
                return group

    def get_group_user_id(self, group_id, user_id):
        user = self.get_user_by_id(user_id)
        for group in user["groups_users"]:
            if group["group_id"] == group_id:
                return group["id"]

    def update_user_to_group_admin(self, group_id, user_id):
        group_user_id = self.get_group_user_id(group_id, user_id)

        post = {
            "id": group_id,
            "groups_users": [{"id": group_user_id, "is_admin": True}],
        }
        url = f"{self.base_url}/groups/{group_id}/dry-run.json"
        response = self.session.put(url, json=post)
        if response.status_code == 200:
            url = f"{self.base_url}/groups/{group_id}.json"
            response = self.session.put(url, json=post)
        else:
            print(response.headers)
            print()
            print(response.text)
            print()

        return response

    def get_user_public_key(self, user_id):
        url = f"{self.base_url}/users/{user_id}.json"
        response = self.session.get(url)

        user = json.loads(response.text)["body"]
        return user["gpgkey"]

    def get_resource_secret(self, resourceId):
        url = f"{self.base_url}/secrets/resource/{resourceId}.json"
        response = self.session.get(url)

        secrete_data = json.loads(response.text)["body"]["data"]
        return secrete_data

    def get_resources(self):
        url = f"{self.base_url}/resources.json"
        response = self.session.get(url)

        secrete_data = json.loads(response.text)["body"]
        return secrete_data

    def get_resource_per_uuid(self, uuid):
        url = f"{self.base_url}/resources/{uuid}.json"
        response = self.session.get(url)

        secrete_data = json.loads(response.text)["body"]
        return secrete_data
