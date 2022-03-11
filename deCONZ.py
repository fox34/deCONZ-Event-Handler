import asyncio
from datetime import datetime
import logging
import math
import json
import requests
import signal
import sys
import time
import threading
import websockets

# TODO Letzte Sensorbewegung aus API abrufen und Licht entsprechend ausschalten?
logger = logging.getLogger('deconz')

class EventHandler:
    def validate(self) -> None:
        pass
    
    async def handle(self, event: dict) -> None:
        pass


class SensorEventHandler(EventHandler):
    
    # Konfiguration
    sensor_id = None
    area_name = None
    target_group = None # Ziel = Gruppe von Lichtern (Gruppen-ID)
    target_id = None # ODER: Ziel = Einzelnes Licht (Licht-ID)
    brightness_values = None
    
    # Wann nach letzter Bewegung herunterdimmen? (min)
    timeout_dimming = 2
    
    # Wie langsam herunterdimmen (s)
    transition_time_dimming = 30
    
    # Wann nach dem Dimmen abschalten (min)
    timeout_turn_off = 2
    
    
    # Zustandsvariablen
    _prev_brightness = None
    _timer = None
    _dry_run = False
    
    
    def __init__(self, config: dict, dry_run: bool = False):
        self.api_base_url = (
            f"http://{config['deCONZ']['host']}:{config['deCONZ']['RESTPort']}"
            f"/api/{config['deCONZ']['username']}"
        )
        self._dry_run = dry_run
        
    
    '''Konfiguration überprüfen'''
    def validate(self) -> None:
        if not (
            self.sensor_id is not None and
            self.area_name is not None and
            self.brightness_values is not None and
            (self.target_group is not None or self.target_id is not None)
        ):
            logger.error("Event-Handler: Konfigurationsfehler, es fehlen Variablen.")
            raise Exception("Nicht alle nötigen Variablen wurden definiert: "
                            "sensor_id, area_name, brightness_values und target_group oder target_id.")
        
        # Zur Initialisierung alle Lichter abschalten, um Timer ggf. neu zu setzen
        self.turn_off()
        
    
    '''Ziel-Adresse für das Ziel-Licht oder die Ziel-Gruppe erzeugen'''
    @property
    def target_url(self):
        if self.target_id is not None:
            return f"{self.api_base_url}/lights/{self.target_id}"
        elif self.target_group is not None:
            return f"{self.api_base_url}/groups/{self.target_group}"
        else:
            logger.error(f"{self.area_name}: Weder target_id noch target_group sind definiert.")
            raise Exception(f"{self.area_name}: Weder target_id noch target_group sind definiert.")
    
    
    '''Ziel-Helligkeit berechnen'''
    def calculate_target_brightness(self, dim_down: bool) -> int:
        
        # dim_down: True / False
        # -> Nach und nach dunkler werden, wenn keine Bewegung mehr erkannt wurde
        currentTime = datetime.now().time()
        target_level = 0
        for startTime, level in self.brightness_values.items():
            if currentTime >= startTime:
                target_level = level
        
        # Ziel soll gedimmt werden: Halbieren
        if dim_down:
            target_level = math.ceil(target_level / 2)
        
        return target_level
    
    
    '''Fehlgeschlagene PUT-Requests bis zu num_tries mal versuchen'''
    def ensure_request(self, max_retries: int = 10, **kwargs) -> requests.Request:
        num_tries = 0
        while True:
            num_tries = num_tries + 1
            if num_tries > max_retries:
                logger.error(f"Anfrage nach {max_tries} Versuchen nicht erfolgreich, Abbruch.")
                raise Exception(f"Anfrage nach {max_tries} Versuchen nicht erfolgreich, Abbruch.")
            
            try:
                r = requests.request(**kwargs, timeout=1)
                if r.status_code == 200:
                    return r
                
                logger.warning(
                    f"{self.area_name}: Fehler {r.status_code}: {r.text}. "
                    "Versuche nach 1s erneut..."
                )
                
                # Möglich: 400, 403, 404, 503
                # Dieser Fehler wird sich auch durch erneute Versuche nicht beheben lassen...
                if r.status_code != 503:
                    logger.error("Fehler durch erneute Versuche nicht behebbar. Abbruch.")
                    raise Exception("Fehler durch erneute Versuche nicht behebbar. Abbruch.")
            
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                logger.warning(
                    f"{self.area_name}: Timeout bei Anfrage an deCONZ. "
                    "Versuche nach {num_tries}s erneut..."
                )
            
            time.sleep(num_tries)
    
    
    def ensure_request_get(self, url: str, **kwargs) -> requests.Request:
        return self.ensure_request(method="GET", url=url, **kwargs)
    
    
    def ensure_request_put(self, url: str, data: dict, **kwargs) -> requests.Request:
        return self.ensure_request(method="PUT", url=url, data=data, **kwargs)
    
    
    
    '''Bewegung erkannt: Licht auf volle Helligkeit stellen'''
    def turn_on(self) -> None:
        
        # Laufenden Dimmer deaktivieren
        if self._timer != None:
            logger.info(f"{self.area_name}: Timer zurückgesetzt.")
            self._timer.cancel()
            self._timer = None
        
        # Zielwert berechnen
        target_level = self.calculate_target_brightness(dim_down=False)
        
        logger.info(f"{self.area_name}: Stelle Licht auf {round(target_level / 255 * 100)} %")
        
        if not self._dry_run:
            data = json.dumps({"on": True, "bri": target_level})
            self.ensure_request_put(f"{self.target_url}/action", data)
        
        # Letzten Wert merken
        self._prev_brightness = target_level
        
        if target_level <= 2:
            # Bereits auf niedrigstem Niveau: Abschalten nach Ablauf der Zeit
            self._timer = threading.Timer(self.timeout_dimming * 60, self.turn_off)
        else:        
            # Dimmen nach Ablauf der Zeit
            self._timer = threading.Timer(self.timeout_dimming * 60, self.dim)
        
        self._timer.start()
    
    
    '''Herunterdimmen. Wird durch self._timer aufgerufen.'''
    def dim(self, target_level : int = None, soft_off : bool = False) -> None:
        self._timer = None
        
        # Aktuellen Zustand abrufen
        state = self.ensure_request_get(self.target_url).json()
        
        # Licht ist aus / Helligkeit weicht von gesetztem Wert ab: Offenbar
        # wurde händisch eingegriffen. Abbruch.
        try:
            if not state['state']['all_on'] or state['action']['bri'] != self._prev_brightness:
                logger.info(
                    f"{self.area_name}: Eingriff erkannt "
                    f"(An: {state['state']['all_on']} / "
                    f"Helligkeit: {round(state['action']['bri']/255*100)} %). "
                    f"Timer beendet."
                )
                return
        except:
            logger.warning(
                "f{self.area_name} (dim): "
                "State (all_on) bzw. Helligkeit (bri) konnte nicht aus Antwort gelesen werden!"
            )
            logger.debug(state)
        
        
        # Zielwert berechnen
        if target_level is None:
            target_level = self.calculate_target_brightness(dim_down=True)
                
        logger.info(f"{self.area_name}: Dimme auf {round(target_level / 255 * 100)} %")
        
        # transitiontime = "Transition time in 1/10 seconds between two states."
        if not self._dry_run:
            data = json.dumps({"bri": target_level, "transitiontime": self.transition_time_dimming*10})
            self.ensure_request_put(f"{self.target_url}/action", data)
        
        self._prev_brightness = target_level
        
        # Abschalt-Timer starten
        if target_level > 2:
            # Nach Ablauf des nächsten Timers: Sanft abschalten
            self._timer = threading.Timer(self.timeout_turn_off * 60, self.dim, (2, True))
        
        elif soft_off:
            # Sanftes Abschalten: Lichter anschließend komplett ausschalten
            self._timer = threading.Timer(self.transition_time_dimming + 1, self.turn_off)
        
        else:
            # Ohne Dimmer bereits auf niedrigstem Wert: Nach dem Timer komplett abschalten
            self._timer = threading.Timer(self.timeout_turn_off * 60, self.turn_off)

        self._timer.start()
    
    
    '''Komplett ausschalten. Wird durch self._timer aufgerufen.'''
    def turn_off(self) -> None:
        self._timer = None
        logger.info(f"{self.area_name}: Schalte aus")
        
        if not self._dry_run:
            data = json.dumps({"on": False})
            self.ensure_request_put(f"{self.target_url}/action", data)
        
    
    '''Auf Event reagieren.'''
    async def handle(self, event: dict) -> None:
        # Alle Events: Dict mit den Feldern
        #   e=changed, id, r=sensors, t=event, uniqueid
        #
        # Variante 1: Sensor-Event "ZHAPresence"
        # Feld "attr" ist vorhanden und enthält als Dict die Felder
        #   id, lastannounced (NULL), lastseen, type=ZHAPresence, Info zur Hardware, uniqueid
        #
        # Variante 2: Presence-Event von deCONZ
        # Feld "state" ist vorhanden und enthält als Dict die Felder
        #   lastupdated, presence
        #
        # Entprellung rund 1min, unabhängig von deCONZ-Einstellungen
        if not ('state' in event and 'presence' in event['state']):
            # Kein deCONZ-Event, ignorieren
            return
        
        if not event['state']['presence']:
            return
        
        logger.info(f"{self.area_name}: Bewegung erkannt")
        
        # Einschalten bzw. Timer zurücksetzen
        self.turn_on()


# Besser als websockets ggf: https://github.com/Kane610/deconz/blob/master/pydeconz/websocket.py
class WebsocketHandler:

    # Zustandsvariablen
    sensor_handlers = {}
    sigterm_received = False
    websocket = None
    
    def __init__(self, config: dict):
        self.websocket_uri = f"ws://{config['deCONZ']['host']}:{config['deCONZ']['websocketPort']}"
        for signame in ('SIGINT', 'SIGTERM'):
            signal.signal(getattr(signal, signame), self.exit_gracefully)
    
    
    '''Event-Handler registrieren'''
    def registerSensorHandler(self, handler: EventHandler) -> None:
        handler.validate()
        if not handler.sensor_id in self.sensor_handlers:
            self.sensor_handlers[handler.sensor_id] = []
        self.sensor_handlers[handler.sensor_id].append(handler)
        logger.debug(f"Event-Handler für Sensor {handler.area_name} "
                      f"({handler.sensor_id}) registriert.")
    
    
    '''SIGTERM/SIGINT abfangen'''
    def exit_gracefully(self, *args) -> None:
        if self.sigterm_received:
            if input("Harten Abbruch erzwingen (y/N): ") == "y":
                logger.warning("Harter Abbruch.")
                sys.exit(1)
            return
        
        # Weicher Abbruch
        logger.info("SIGTERM/SIGKILL empfangen, beende...")
        self.sigterm_received = True
        
        for handlers in self.sensor_handlers.values():
            for handler in handlers:
                if handler._timer != None:
                    logger.info(f"{handler.area_name}: Timer abgebrochen.")
                    handler._timer.cancel()
        
        # Verbindung zu Websocket beenden
        if self.websocket is not None:
            logger.info("Trenne Verbindung...")
            loop = asyncio.get_running_loop()
            loop.create_task(self.exit_event_loop_gracefully())
    
    
    '''Websocket schließen'''
    async def exit_event_loop_gracefully(self) -> None:
        await self.websocket.close()
        logger.info("Verbindung getrennt.")
    
    
    '''Kontrollschleife'''
    async def controlLoop(self) -> None:
        
        loop = asyncio.get_running_loop()
        connect_attempt_counter = 0
        was_connected = False
        while True:
            if self.sigterm_received:
                sys.exit(0)
            
            connect_attempt_counter = connect_attempt_counter + 1
            if connect_attempt_counter > 10:
                logger.error("Konnte keine Verbindung in 10 Versuchen herstellen. Abbruch.")
                sys.exit(1)
            
            try:
                async with websockets.connect(self.websocket_uri, close_timeout=3) as websocket:
                    self.websocket = websocket
                    was_connected = True
                    connect_attempt_counter = 1
                    logger.info(f"Verbindung zu deCONZ-Websocket hergestellt.")
            
                    async for rawMessage in websocket:
                        message = json.loads(rawMessage)
                        if message['r'] == "sensors" and int(message['id']) in self.sensor_handlers:
                            for handler in self.sensor_handlers[int(message['id'])]:
                                loop.create_task(handler.handle(message))
                        
                        if self.sigterm_received:
                            await websocket.close()
                        
                    # end: rawMessage in ws
                # end: with ws as ...
            
            except websockets.ConnectionClosed:
                self.websocket = None
                timeout = 2 * connect_attempt_counter
                logger.warning(
                    f"Websocket-Verbindung beendet. Versuche Wiederaufbau nach {timeout}s "
                    f"(#{connect_attempt_counter})"
                )
                await asyncio.sleep(timeout)
            
            except ConnectionRefusedError:
                logger.warning(f"Keine Verbindung möglich.")
                if was_connected:
                    self.websocket = None
                    timeout = 2 * connect_attempt_counter
                    logger.info(f"Versuche Wiederaufbau nach {timeout}s. (#{connect_attempt_counter})")
                    await asyncio.sleep(timeout)
                else:
                    logger.info("Abbruch.")
                    return
