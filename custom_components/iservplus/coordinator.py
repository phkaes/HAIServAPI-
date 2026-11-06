from datetime import datetime, timedelta
import email
from email import policy
import imaplib
import logging
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_COURSE_FILTER, CONF_HOST, CONF_LETTERS_UNREAD_ONLY, CONF_PASSWORD, CONF_USERNAME, DEFAULT_SCAN_INTERVAL, DOMAIN
from .iserv_lib import IServAPI

_LOGGER = logging.getLogger(__name__)

class IServDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=DEFAULT_SCAN_INTERVAL)
        self.entry = entry
        self.host = entry.data[CONF_HOST]
        self.username = entry.data[CONF_USERNAME]
        self.password = entry.data[CONF_PASSWORD]
        self.course_filter = entry.data.get(CONF_COURSE_FILTER, "")
        self.api = None
        # Cache: Brief-ID -> Inhalt. Briefe ändern sich nicht, daher
        # wird jeder Brief nur einmal nachgeladen (schont den Server).
        self._letter_cache = {}

    @property
    def letters_unread_only(self) -> bool:
        """Option: nur ungelesene Elternbriefe anzeigen.

        Options (nachträglich über 'Konfigurieren' geändert) haben Vorrang
        vor den Daten aus der Ersteinrichtung.
        """
        return bool(
            self.entry.options.get(
                CONF_LETTERS_UNREAD_ONLY,
                self.entry.data.get(CONF_LETTERS_UNREAD_ONLY, False),
            )
        )

    @staticmethod
    def _is_unread(letter: dict) -> bool:
        return letter.get("read") is False or letter.get("acknowledged") is False

    def _extract_body(self, msg) -> str:
        body_text = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body_text = part.get_content()
                    break
        else:
            if msg.get_content_type() == "text/plain":
                body_text = msg.get_content()
        return body_text[:250] if body_text else ""

    def _letter_link(self, letter: dict) -> str | None:
        """Ermittelt die URL zum Brief aus dem Notification-Objekt."""
        for key in ("target", "url", "link", "route", "href"):
            val = letter.get(key)
            if isinstance(val, str) and val:
                if val.startswith("http"):
                    return val
                if val.startswith("/"):
                    return f"https://{self.host}{val}"
                return f"https://{self.host}/{val}"
        # Fallback: Standard-URL des Elternbrief-Moduls aus der ID bauen
        letter_id = letter.get("objectId") or letter.get("referenceId")
        if letter_id:
            return f"https://{self.host}/iserv/parentletter/letter/{letter_id}"
        return None

    def _fetch_letter_content(self, letter: dict) -> str | None:
        """Lädt den Volltext eines Elternbriefs nach (JSON oder HTML)."""
        link = self._letter_link(letter)
        if not link:
            return None
        try:
            # 1. Versuch: JSON-Antwort (manche Module antworten auf XHR mit JSON)
            res = self.api._session.get(
                link,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                },
            )
            if res.status_code != 200:
                _LOGGER.debug("Elternbrief %s lieferte Status %s", link, res.status_code)
                return None

            if "json" in res.headers.get("Content-Type", ""):
                j = res.json() or {}
                candidates = [j]
                if isinstance(j.get("data"), dict):
                    candidates.append(j["data"])
                for obj in candidates:
                    for k in ("text", "body", "content", "message", "letterText"):
                        if isinstance(obj.get(k), str) and obj[k].strip():
                            return obj[k].strip()
                return None

            # 2. Versuch: HTML-Seite parsen
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, "html.parser")
            main = (
                soup.select_one(".parentletter-content")
                or soup.select_one(".letter-content")
                or soup.select_one("div.panel-body")
                or soup.select_one("main")
                or soup.select_one("#content")
            )
            if main:
                # Navigation/Skripte entfernen, nur lesbaren Text zurückgeben
                for tag in main.select("script, style, nav, header, footer"):
                    tag.decompose()
                text = main.get_text("\n", strip=True)
                return text if text else None
        except Exception as e:
            _LOGGER.debug("Inhalt von Elternbrief %s nicht abrufbar: %s", link, e)
        return None

    def _sync_fetch_data(self):
        if not self.api:
            self.api = IServAPI(self.username, self.password, self.host)

        data = {}
        now = datetime.now()
        headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}

        # 1. Stundenplan (Aktuelle Woche + 3 Folgewochen = 4 Wochen gesamt)
        monday = now.date() - timedelta(days=now.weekday())
        timetable = {}

        for week_offset in range(4):
            current_monday = monday + timedelta(days=week_offset * 7)
            api_url = f"https://{self.host}/iserv/dieschulapp/api/1.0/current-timetable/"
            params = {
                "date": current_monday.strftime("%Y-%m-%d"), 
                "week": "true", 
                "substitutions": "true"
            }
            if self.course_filter:
                params["filterBy"] = f"courseSubject.course:in({self.course_filter})"
            
            res = self.api._session.get(api_url, params=params, headers=headers)
            
            if res.status_code == 200:
                res_data = res.json() or {}
                entries = res_data.get("entries") or []
                
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                        
                    weekday = entry.get("weekday")
                    if weekday is None or weekday > 4:
                        continue
                    
                    lesson_date = current_monday + timedelta(days=weekday)
                    date_str = lesson_date.strftime("%Y-%m-%d")

                    if date_str not in timetable:
                        timetable[date_str] = []

                    slot = entry.get("timeTableSlot") or {}
                    course_sub = entry.get("courseSubject") or {}
                    subject = (course_sub.get("subject") or {}).get("name", "Unbekannt")
                    room_data = entry.get("room") or {}
                    room = room_data.get("name", "")
                    
                    sub_type = entry.get("substitutionType")
                    
                    status = "REGULÄR"
                    if sub_type:
                        status = "ENTFALL" if sub_type == "canceled" else "VERTRETUNG"
                    
                    timetable[date_str].append({
                        "slot": slot.get("number", 0),
                        "time": f"{slot.get('startTime', '')}-{slot.get('endTime', '')}",
                        "course": subject,
                        "room": room,
                        "status": status,
                        "info": entry.get("message") or ""
                    })
        
        for date_str in timetable:
            timetable[date_str].sort(key=lambda x: x.get("slot", 0))
            
        data["timetable"] = timetable

        # 2. Termine
        try:
            events = self.api.get_upcoming_events()
            data["events"] = events.get("events", events.get("data", [])) if isinstance(events, dict) else (events if isinstance(events, list) else [])
        except Exception:
            data["events"] = []

        # 3. Benachrichtigungen
        try:
            notifs = self.api.get_notifications()
            data["notifications"] = notifs.get("notifications", notifs.get("data", [])) if isinstance(notifs, dict) else (notifs if isinstance(notifs, list) else [])
        except Exception:
            data["notifications"] = []

        # 4. Elternbriefe (Neu gefiltert aus der v1 Notification API, wie in der HAR-Datei gefunden)
        try:
            letters_url = f"https://{self.host}/iserv/notification/api/v1/notifications"
            res_letters = self.api._session.get(letters_url, headers=headers)
            
            if res_letters.status_code == 200:
                json_data = res_letters.json() or {}
                notifications = []
                
                if isinstance(json_data.get("data"), dict):
                    notifications = json_data["data"].get("notifications", [])
                elif isinstance(json_data.get("notifications"), list):
                    notifications = json_data["notifications"]
                elif isinstance(json_data, list):
                    notifications = json_data
                
                # Filtere alle Benachrichtigungen heraus, die den Typ oder die Gruppe "parentletter" haben
                data["elternbriefe"] = [
                    n for n in notifications 
                    if isinstance(n, dict) and (n.get("type") == "parentletter" or n.get("groupId") == "parentletter")
                ]

                # NEU: Je nach Konfiguration nur ungelesene Briefe behalten.
                # Der Filter läuft VOR dem Nachladen der Inhalte, damit für
                # bereits gelesene Briefe gar keine Requests anfallen.
                if self.letters_unread_only:
                    data["elternbriefe"] = [
                        b for b in data["elternbriefe"] if self._is_unread(b)
                    ]

                # NEU: Volltexte nachladen (mit Cache, max. die 10 neuesten Briefe)
                for brief in data["elternbriefe"][:10]:
                    letter_id = str(brief.get("id") or brief.get("uuid") or self._letter_link(brief) or "")
                    if not letter_id:
                        continue
                    if letter_id in self._letter_cache:
                        brief["content"] = self._letter_cache[letter_id]
                        continue
                    content = self._fetch_letter_content(brief)
                    if content:
                        # Recorder-Limit von HA: max. 16 KB Attribute pro Entität.
                        content = content[:2000]
                        self._letter_cache[letter_id] = content
                        brief["content"] = content
            else:
                data["elternbriefe"] = []
        except Exception as e:
            _LOGGER.warning("Fehler beim Abrufen der Elternbriefe über die Notification API: %s", e)
            data["elternbriefe"] = []

        # 5. E-Mails via IMAP
        emails = []
        try:
            mail = imaplib.IMAP4_SSL(self.host, 993)
            mail.login(self.username, self.password)
            mail.select("INBOX")
            _, search_data = mail.search(None, "UNSEEN")
            msg_ids = search_data[0].split()
            for num in reversed(msg_ids[:5]):
                _, msg_data = mail.fetch(num, "(BODY.PEEK[])")
                parsed_msg = email.message_from_bytes(msg_data[0][1], policy=policy.default)
                emails.append({
                    "subject": str(parsed_msg.get("Subject", "Kein Betreff")),
                    "sender": str(parsed_msg.get("From", "Unbekannt")),
                    "date": str(parsed_msg.get("Date", "")),
                    "body": self._extract_body(parsed_msg)
                })
            mail.close()
            mail.logout()
        except Exception as e:
            _LOGGER.warning("IMAP Fehler: %s", e)
        
        data["emails"] = emails
        data["email_count"] = len(emails)

        return data

    async def _async_update_data(self):
        try:
            return await self.hass.async_add_executor_job(self._sync_fetch_data)
        except Exception as err:
            raise UpdateFailed(f"Fehler beim IServ Abruf: {err}")
