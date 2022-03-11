#!/usr/bin/env python3

# Beobachte deCONZ-Websocket auf Sensor-Ereignisse und schalte Licht entsprechend an/aus

import asyncio
import argparse
from datetime import datetime, time
import deCONZ
import logging
import json
import os
import sys

if __name__ != "__main__":
    print("Kann nicht als Modul geladen werden.")
    sys.exit(1)

__location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))


# Eingabeargumente lesen
parser = argparse.ArgumentParser(description='deCONZ Ereignis-Steuerung')
parser.add_argument("-v", "--verbose", action="store_true",
                    help="Debug-Ausgabe aktivieren")
parser.add_argument("-n", "--no-action", action="store_true",
                    help="Keine Steuerung durchführen (nur anzeigen, was getan werden würde)")
args = parser.parse_args()


# Logging einrichten
loglevel = logging.INFO
logger = logging.getLogger('deconz')
if args.verbose:
    logger.setLevel(logging.DEBUG)

dateformat = "[%d.%m.%Y %H:%M:%S]"
if os.isatty(sys.stdout.fileno()):
    logging.basicConfig(format="%(asctime)s %(message)s", level=loglevel, datefmt=dateformat)
    logger.debug("Log-Ausgabe in Terminal.")
else:
    logging.basicConfig(filename="/var/log/lichtsteuerung/deconz.log",
                        format="%(asctime)s %(message)s", level=loglevel, datefmt=dateformat)
    logger.debug("Log-Ausgabe in Logdatei.")

if args.verbose:
    logger.debug("Ausführliche Debug-Ausgabe aktiviert.")

if args.no_action:
    logger.info("Steuerung deaktiviert.")


# Konfiguration lesen
try:
    with open(os.path.join(__location__, "config.json"), "rb") as fp:
        config = json.load(fp)
except OSError as e:
    logger.error(f"Konnte Konfiguration nicht einlesen: {e}")
    sys.exit(1)


# Start
logger.info("Starte Lichtsteuerung...")

# Websocket-Handler initialisieren
websocket_handler = deCONZ.WebsocketHandler(config)


# Event-Handler initialisieren

# Beispiel: Raum 1
# ID des Sensors: 10
# ID der Lichtgruppe: 8
room_1 = deCONZ.SensorEventHandler(config, args.no_action)
room_1.area_name = "Raum 1"
room_1.sensor_id = 10
room_1.target_group = 8
room_1.brightness_values = {
    
    # Ab Mitternacht: Orientierungslicht
    # Wert "1" schaltet das Licht bei diesen Leuchten ab (Hue White)
    time(hour=0): 2,
    
    # Ab 07:30: Tageslicht
    time.fromisoformat("07:30"): 255,
    
    # Ab 21:00: Leicht gedimmt
    time.fromisoformat("21:00"): 128,
    
    # Ab 23:00: Orientierungslicht
    time.fromisoformat("23:00"): 2
}

websocket_handler.registerSensorHandler(room_1)
logger.debug(f"Steuerung für Raum 1 hinzugefügt.")


# Beispiel: Raum 2
# ID des Sensors: 20
# ID des einzelnen Lichts: 23
room_2 = deCONZ.SensorEventHandler(config, args.no_action)
room_2.area_name = "Raum 2"
room_2.sensor_id = 20
room_2.target_id = 23
room_2.brightness_values = {

    # Ab Mitternacht: Orientierungslicht
    # Hier ist der Wert 1 der Minimalwert (IKEA)
    time(hour=0): 1,
    
    # Ab 06:30: Tageslicht
    time.fromisoformat("06:30"): 255
}

websocket_handler.registerSensorHandler(room_2)
logger.debug(f"Steuerung für Raum 2 hinzugefügt.")


# Websocket-Handler starten
asyncio.run(websocket_handler.controlLoop())
