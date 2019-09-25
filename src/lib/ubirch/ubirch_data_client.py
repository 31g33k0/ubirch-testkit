import binascii
import time
from uuid import UUID

import umsgpack as msgpack
import urequests as requests

from .ubirch_client import UbirchClient


class UbirchDataClient:

    def __init__(self, uuid: UUID, cfg: dict):
        self.__sensor_type = cfg["type"]
        self.__uuid = uuid
        self.__auth = cfg['password']
        self.__data_service_url = cfg['data']
        self.__headers = {'X-Ubirch-Hardware-Id': str(self.__uuid), 'X-Ubirch-Credential': self.__auth}
        self.__msg_type = 0

        # this client will generate a new key pair and register the public key at the key service
        self.__ubirch = UbirchClient(uuid, self.__headers, cfg['keyService'], cfg['niomon'])

    def send(self, data: dict):
        payload = {self.__sensor_type: data}

        # pack data map as message array with uuid, message type and timestamp
        msg = [
            self.__uuid.bytes,
            self.__msg_type,
            int(time.time()),
            payload
        ]

        # convert the message to msgpack format
        serialized = bytearray(msgpack.packb(msg, use_bin_type=True))
        print(binascii.hexlify(serialized))

        # send message to ubirch data service (only send UPP if successful)
        print("** sending measurements to ubirch data service ...")
        r = requests.post(self.__data_service_url, headers=self.__headers, data=serialized)

        if r.status_code == 200:
            # send UPP to niomon
            print("** sending measurement certificate ...")
            self.__ubirch.send(serialized)
        else:
            raise Exception(
                "!! request to {} failed with status code {}: {}".format(self.__data_service_url, r.status_code,
                                                                         r.text))
