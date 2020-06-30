print("*** UBIRCH SIM Testkit ***")
print("++ importing:")

print("\ttime")
import time

# remember wake-up time
print("++ saving boot time")
start_time = time.time()

print("\tmachine")
import machine

# set watchdog: if execution hangs/takes longer than 'timeout' an automatic reset is triggered
# we need to do this as early as possible in case an import cause a freeze for some reason
print("++ enabling watchdog")
wdt = machine.WDT(timeout=5 * 60 * 1000)  # set it
wdt.feed()  # we only feed it once since this code hopefully finishes with deepsleep (=no WDT) before reset_after_ms

print("\tOS")
import os

print("\tconfig")
from config import load_config

print("\tconnection")
from connection import get_connection, NB_IoT

print("\terror handling")
from error_handling import *

print("\tmodem")
from modem import get_imsi

print("\trealtimeclock")
from realtimeclock import *

print("\tnetwork")
from network import LTE

print("\tubirch")
import ubirch

print("\thelpers")
from helpers import *

print("\tbinascii")
from binascii import hexlify  # , b2a_base64, a2b_base64, unhexlify

# Pycom specifics
print("\tpyboard")
from pyboard import get_pyboard

# error color codes
COLOR_INET_FAIL = LED_PURPLE
COLOR_BACKEND_FAIL = LED_ORANGE
COLOR_SIM_FAIL = LED_RED
COLOR_CONFIG_FAIL = LED_YELLOW
COLOR_MODEM_FAIL = LED_BLUE
COLOR_UNKNOWN_FAIL = LED_WHITE

# signal beginning of main code
set_led(LED_GREEN)

# check reset cause
COMING_FROM_DEEPSLEEP = (machine.reset_cause() == machine.DEEPSLEEP_RESET)

# mount SD card if there is one
print("++ mounting SD")
SD_CARD_MOUNTED = mount_sd()
if SD_CARD_MOUNTED:
    print("\tSD card mounted")
else:
    print("\tno SD card found")

#############
#   SETUP   #
#############

# set up error handling
max_file_size_kb = 2000 if SD_CARD_MOUNTED else 20
error_handler = ErrorHandler(file_logging_enabled=True, max_file_size_kb=max_file_size_kb,
                             sd_card=SD_CARD_MOUNTED)
try:
    # initialize globals
    lte = LTE()

    try:
        # do modem reset on any non-normal loop (modem might be in a strange state)
        if not COMING_FROM_DEEPSLEEP:
            print("++ not coming from sleep, resetting modem")
            reset_modem(lte)

        print("++ getting IMSI")
        imsi = get_imsi(lte)
        print("IMSI: " + imsi)
    except Exception as e:
        print("\tERROR setting up modem")
        error_handler.log(e, COLOR_MODEM_FAIL)
        while True:
            machine.idle()

    if not COMING_FROM_DEEPSLEEP and SD_CARD_MOUNTED: store_imsi(imsi)

    # load configuration, blocks in case of failure
    print("++ loading config")
    try:
        cfg = load_config(sd_card_mounted=SD_CARD_MOUNTED)

        lvl_debug = cfg['debug']  # set debug level
        if lvl_debug: print("\t" + repr(cfg))

        interval = cfg['interval']  # set measurement interval
        sensors = get_pyboard(cfg['board'])  # initialise the sensors on the pyboard
        connection = get_connection(lte, cfg)  # initialize connection object depending on config
        api = ubirch.API(cfg)  # set up API for backend communication
    except Exception as e:
        print("\tERROR loading configuration")
        error_handler.log(e, COLOR_CONFIG_FAIL)
        while True:
            machine.idle()

    # get pin from flash, or bootstrap from backend and save
    pin_file = imsi + ".bin"
    pin = get_pin_from_flash(pin_file, imsi)
    if pin is None:
        try:
            connection.connect()
        except Exception as e:
            error_handler.log(e, COLOR_INET_FAIL, reset=True)

        try:
            pin = bootstrap(imsi, api)
            with open(pin_file, "wb") as f:
                f.write(pin.encode())
        except Exception as e:
            error_handler.log(e, COLOR_BACKEND_FAIL, reset=True)

    # disconnect from LTE connection before accessing SIM application
    if isinstance(connection, NB_IoT):
        print("\tdisconnecting")
        connection.disconnect()

    # initialise ubirch SIM protocol
    print("++ initializing ubirch SIM protocol")
    try:
        sim = ubirch.SimProtocol(lte=lte, at_debug=lvl_debug)
    except Exception as e:
        error_handler.log(e, COLOR_SIM_FAIL, reset=True)

    # unlock SIM
    try:
        sim.sim_auth(pin)
    except Exception as e:
        error_handler.log(e, COLOR_SIM_FAIL)
        # if pin is invalid, there is nothing we can do -> block
        while True:
            print("PIN is invalid, can't continue")
            set_led(COLOR_SIM_FAIL)
            time.sleep(0.5)
            set_led(0)
            time.sleep(0.5)

    # get UUID from SIM
    key_name = "ukey"
    uuid = sim.get_uuid(key_name)
    print("UUID: " + str(uuid))

    # # send a X.509 Certificate Signing Request for the public key to the ubirch identity service
    csr_file = "csr_{}_{}.der".format(uuid, api.env)
    if csr_file not in os.listdir():
        try:
            connection.connect()
        except Exception as e:
            error_handler.log(e, COLOR_INET_FAIL, reset=True)

        try:
            csr = submit_csr(key_name, cfg["CSR_country"], cfg["CSR_organization"], sim, api)
            with open(csr_file, "wb") as f:
                f.write(csr)
        except Exception as e:
            error_handler.log(e, COLOR_BACKEND_FAIL)

    # check if the board has a time set, if not synchronize it
    print("++ checking board time\n\ttime is: ", board_time())
    if not board_time_valid():  # time can't be correct -> connect to sync time
        print("\ttime invalid, syncing")
        # connect to network, set time, disconnect afterwards to speed up SIM communication
        try:
            connection.connect()
            enable_time_sync()
            print("\twaiting for time sync")
            wait_for_sync(print_dots=False)
        except Exception as e:
            error_handler.log(e, COLOR_INET_FAIL, reset=True)

    if isinstance(connection, NB_IoT):
        print("\tdisconnecting")
        connection.disconnect()

    ############
    #   DATA   #
    ############

    # get data from sensors
    print("++ getting measurements")
    data = sensors.get_data()
    # print_data(data)

    # pack data message containing measurements, device UUID and timestamp to ensure unique hash
    print("++ packing data")
    message = pack_data_json(uuid, data)
    print("\tdata message [json]: {}\n".format(message.decode()))

    # seal the data message (data message will be hashed and inserted into UPP as payload by SIM card)
    print("++ creating UPP")
    upp = sim.message_chained(key_name, message, hash_before_sign=True)
    print("\tUPP: {}\n".format(hexlify(upp).decode()))

    # retrieve data message hash from generated UPP for verification
    # message_hash = get_upp_payload(upp)
    # print("\tdata message hash: {}".format(b2a_base64(message_hash).decode()))

    ###############
    #   SENDING   #
    ###############

    print("++ checking/establishing connection")
    try:
        connection.connect()
        enable_time_sync()
    except Exception as e:
        error_handler.log(e, COLOR_INET_FAIL, reset=True)

    # send data to ubirch data service and UPP to ubirch auth service
    # TODO: add retrying to send/handling of already created UPP in case of final failure

    try:
        # send data message to data service, with reconnects/modem resets if necessary
        print("++ sending data")
        status_code, content = send_backend_data(sim, lte, connection, api.send_data, uuid, message)

        # communication worked in general, now check server response
        if not 200 <= status_code < 300:
            raise Exception("backend (data) returned error: ({}) {}".format(status_code, str(content)))

        # send UPP to the ubirch authentication service to be anchored to the blockchain
        print("++ sending UPP")
        status_code, content = send_backend_data(sim, lte, connection, api.send_upp, uuid, upp)

        # communication worked in general, now check server response
        if not 200 <= status_code < 300:
            raise Exception("backend (UPP) returned error:: ({}) {}".format(status_code, str(content)))

    except Exception as e:
        error_handler.log(e, COLOR_BACKEND_FAIL)

    print("++ waiting for time sync")
    try:
        wait_for_sync(print_dots=True, timeout=10)
        print("\ttime synced")
    except Exception as e:
        error_handler.log("WARNING: Could not sync time before timeout: {}".format(repr(e)), COLOR_INET_FAIL)

    ###################
    #   GO TO SLEEP   #
    ###################

    # prepare hardware for sleep (needed for low current draw and
    # freeing of resources for after the reset, as the modem stays on)
    print("++ preparing hardware for deepsleep")
    print("\tclose connection")
    connection.disconnect()

    print("\tdeinit SIM")
    sim.deinit()

    # not detaching causes smaller/no re-attach time on next reset but but
    # somewhat higher sleep current needs to be balanced based on your specific interval
    print("\tdeinit LTE")
    lte.deinit(detach=False)

    # go to deepsleep
    sleep_time = interval - int(time.time() - start_time)
    if sleep_time < 0:
        sleep_time = 0
    print(">> going into deepsleep for {} seconds".format(sleep_time))
    set_led(LED_OFF)
    machine.deepsleep(1000 * sleep_time)  # sleep, execution will resume from main.py entry point

except Exception as e:
    error_handler.log(e, COLOR_UNKNOWN_FAIL, reset=True)
