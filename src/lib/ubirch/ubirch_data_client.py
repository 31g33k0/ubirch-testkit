from uuid import UUID
import umsgpack as msgpack
import urequests as requests
import time

from .ubirch_client import UbirchClient


class UbirchDataClient:

    def __init__(self, uuid: UUID, cfg: dict):
        self.__uuid = uuid
        self.__auth = cfg['password']
        self.__data_service_url = cfg['data']
        self.__headers = {'X-Ubirch-Hardware-Id': str(self.__uuid), 'X-Ubirch-Credential': self.__auth}

        # this client will generate a new key pair and register the public key at the key service
        self.__ubirch = UbirchClient(uuid, self.__headers, cfg['keyService'], cfg['niomon'])

    def send(self, data: dict):
        print("** sending measurements to ubirch data service ...")

        # pack data map as message with uuid and timestamp
        msg = {'uuid': self.__uuid.bytes, 'timestamp': int(time.time()), 'data': data}

        # convert the message to msgpack format
        serialized = bytearray(msgpack.packb(msg, use_bin_type=True))
        # print(binascii.hexlify(serialized))

        # send message to ubirch data service
        r = requests.post(self.__data_service_url, headers=self.__headers, data=serialized)

        if r.status_code == 200:
            print("** success")
        else:
            print("!! request to {} failed with {}: {}".format(self.__data_service_url, r.status_code, r.text))

        # send UPP to niomon
        print("** sending measurement certificate ...")
        self.__ubirch.send(msg)
