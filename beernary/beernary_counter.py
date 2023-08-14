#!/usr/bin/python3

"""
This application is for operation of the beernary's registry component
"""

# standard imports
import sys
import configparser
import signal

# pip packages
import time
import serial
from loguru import logger

try:
    import RPi.GPIO as GPIO
except: # pylint: disable=bare-except
    import Mock.GPIO as GPIO

# custom beernary
import modules.database
import modules.display
import modules.flowsensor
import modules.reader
import modules.valve
import modules.light

CONFIG_FILE_PATH    = "/etc/beernary/config.ini"

global valve    # required for immediately

def exit(*args):
    """Exit function called by SIGTERM for clean rundown"""
    logger.critical("System shutdown initiated via SIGINT")

    valve.unlocked = False

    GPIO.cleanup()
    sys.exit(0)

def main():
    """This function operates the beernary counter application"""

    # Configuration handling
    config                 = configparser.ConfigParser(allow_no_value=True)
    config.read(CONFIG_FILE_PATH)

    log_level               = config.get("system",           "log_level")

    mysql_host              = config.get("mysql",            "hostname")
    mysql_user              = config.get("mysql",            "username")
    mysql_password          = config.get("mysql",            "password")
    mysql_database          = config.get("mysql",            "database")

    signal_light_device     = config.get("serial_devices",   "signal_light")

    rfid_serial_device      = config.get("serial_devices",   "rfid_reader")
    gpio_pin_flowsensor     = int(config.get("gpio_pins",    "flowsensor"))
    gpio_pin_valve          = int(config.get("gpio_pins",    "valve"))
    gpio_pin_valve_2          = int(config.get("gpio_pins",  "valve_2"))

    #  Local variables
    current_user_id         = ""
    current_user_pulses_stx = int   # pulses before draft
    current_user_pulses_etx = int   # pulses after draft

    current_keg_id          = int
    previous_keg_id         = current_keg_id

    event_id                = None
    event_name              = None

    # Initialize logging
    logger.remove(0)                        # remote default logger
    logger.add(sys.stdout, level=log_level, colorize=True)     # add handler with custom log level
    logger.add("./log/beernary-counter.log", level=log_level, colorize=True)     # add handler with custom log level

    # Initalize LCD display
    display                 = modules.display.LCDDisplay()
    display.enabled         = True  # enable backlight

    # Initalize signal light
    signal_light            = modules.light.BeernarySignalLight(signal_light_device, 9600)
    signal_light.send_command(signal_light.GREEN_OFF)
    signal_light.send_command(signal_light.RED_OFF)
    signal_light.send_command(signal_light.RED_ON)

    # Initialize valve
    valve                   = modules.valve.Valve(gpio_pin_valve)

    # Initalize flowsensor
    flowsensor              = modules.flowsensor.PulseFlowsensor(gpio_pin_flowsensor)

    # Reader initialization
    try:
        rfid_reader = modules.reader.RawRfidReader(rfid_serial_device, 9600)

    except serial.serialutil.SerialException:
        logger.critical(f"Could not open serial device: {rfid_serial_device}")
        display.send_message("[E] Serial setup error", 2 ,"ljust")
        sys.exit(1)

    else:
        logger.info(f"Successfully opened serial device: {rfid_serial_device}")

    # Database initialization
    try:
        database   = modules.database.BeernaryMysqlTransaction(host=mysql_host,
                                                               user=mysql_user,
                                                               passwd=mysql_password,
                                                               database=mysql_database)

    except Exception as exception:
        logger.critical(f"Could not open database : {exception}")
        display.send_message("[E] Database failed",2,"ljust")
        sys.exit(1)

    else:
        logger.info(f"Successfully opened database: {mysql_database} at {mysql_host}")

    # Event initialization
    try:
        event_data  = database.get_active_event()
        event_id    = event_data[0]
        event_name  = event_data[1]

    except modules.database.BeernaryTransactionLogicError as exception:
        logger.critical(f"Could not open active event: {exception}")
        display.send_message("[E] Event setup error",2,"ljust")
        sys.exit(1)

    else:
        logger.info(f"Successfully received active event: {event_name}")

    # Keg initialization
    try:
        current_keg_id      = database.get_current_keg(event_id)         # initialize keg ID as in database      pylint: disable=line-too-long
        current_keg_pulses  = database.get_keg_pulses(current_keg_id)    # initialize keg pulses as in databse   pylint: disable=line-too-long
        previous_keg_id     = current_keg_id                             # set initial rotation state            pylint: disable=line-too-long

    except modules.database.BeernaryTransactionLogicError as exception:
        logger.critical(f"Could not get active keg: {exception}")
        display.send_message("[E] Keg setup error ",2,1)
        sys.exit(1)

    else:
        logger.info(f"Successfully received active keg: {current_keg_id}")

    logger.info("Beernary successfully initialized! Welcome home.")

    # Bootscreen
    display.send_message("  Beernary Counter  ",      1,"ljust")
    display.send_message("                    ",      2,"ljust")
    display.send_message("     Welcome to     ",      3,"ljust")
    display.send_message(event_name, 4,"centred")
    time.sleep(5)

    valve.unlocked  = False

    # Main application loop
    while True:

        # Handle keg rotation
        try:
            current_keg_id = database.get_current_keg(event_id)

            # detect rotated keg
            if previous_keg_id != current_keg_id:                               # detect rotation in database based on keg ID               pylint: disable=line-too-long

                current_keg_pulses = database.get_keg_pulses(current_keg_id)    # set pulses for current keg as in database for new keg     pylint: disable=line-too-long
                logger.info(f"Successfully rotated keg {previous_keg_id} to {current_keg_id}")
                previous_keg_id    = current_keg_id                             # finish rotation by adjusting keg ID                       pylint: disable=line-too-long

        except modules.database.BeernaryTransactionLogicError as exception:
            logger.critical(f"Could not get active keg: {exception}")
            display.send_message("[E] Keg setup error ", 2, 1)
            sys.exit(1)

        signal_light.send_command(signal_light.RED_ON)
        display.send_message("   Please scan tag  ",3,"ljust")
        display.send_message("                    ",4,"ljust")

        current_user_id = rfid_reader.read_rfid() # blocking/waiting
        logger.info(f"Received RFID tag: {current_user_id}")

        # Sanity check from reader
        if current_user_id is not None:

            display.enabled = True

            current_user_name = database.check_user(current_user_id)

            # Implicit check for unauthorized user
            if current_user_name is not None:

                logger.info(f"Authorized user: {current_user_name}")

                # "Map" start pulse count to user context
                current_user_pulses_stx = flowsensor.pulses
                logger.debug(f"STX pulse value for {current_user_name}: {current_user_pulses_stx}")

                signal_light.send_command(signal_light.RED_OFF)
                signal_light.send_command(signal_light.GREEN_BLINK)
                display.send_message(f"User {current_user_name}", 3, "ljust")

                valve.unlocked = True

                unlock_time    = 10
                warning_time   = 5
                for i in range (unlock_time):
                    time.sleep(1)
                    display.send_message(f"Draft time left: {unlock_time-i}s",   4, "ljust")

                    if i > warning_time:
                        signal_light.send_command(signal_light.GREEN_OFF)
                        signal_light.send_command(signal_light.YELLOW_ON)

                valve.unlocked = False

                # "Map" stop pulse count to user context
                current_user_pulses_etx = flowsensor.pulses
                logger.debug(f"ETX pulse value for {current_user_name}: {current_user_pulses_etx}")

                try:
                    database.store_draft(current_user_id, current_user_pulses_etx - current_user_pulses_stx)
                    database.set_keg_pulses(current_keg_id, current_user_pulses_etx)
                except modules.database.BeernaryTransactionLogicError as exception:
                    logger.critical(f"Could not store draft: {exception}")
                    sys.exit(1)

                signal_light.send_command(signal_light.YELLOW_OFF)
                signal_light.send_command(signal_light.GREEN_OFF)

            # Handling of unauthorized user
            elif current_user_name is None:
                logger.warning(f"Unauthorized user: {current_user_id}")

                signal_light.send_command(signal_light.RED_OFF)
                signal_light.send_command(signal_light.RED_BLINK)
                display.send_message("Unauthorized access!",   3, "ljust")
                display.send_message("Staff is informed",      4, "ljust")

                signal_light.send_command(signal_light.BUZZER_ON)
                time.sleep(0.2)
                signal_light.send_command(signal_light.BUZZER_OFF)
                time.sleep(0.2)
                signal_light.send_command(signal_light.BUZZER_ON)
                time.sleep(0.2)
                signal_light.send_command(signal_light.BUZZER_OFF)

                time.sleep(2)
                signal_light.send_command(signal_light.RED_OFF)

        rfid_reader.flush_queue()

if __name__ == '__main__':

    signal.signal(signal.SIGTERM, exit)

    try:
        main()

    except Exception as exception:
        logger.critical(f"System error: {exception}")
        raise
