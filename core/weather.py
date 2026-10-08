"""Weather and temperature, free and key-less: Open-Meteo forecasts, their geocoder, and IP location.

    "what's the temperature"  "what's the weather like in Paris"  "is it going to rain tomorrow"
    "do I need a jacket"  "what's the forecast for this weekend"  "how hot is it outside"

Where you are comes from (in order): the place you asked about, Settings ▸ Weather location, what
JARVIS remembers ("You live in Madrid"), or your internet connection's approximate location.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

log = logging.getLogger("jarvis.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
IP_LOOKUPS = ("https://ipwho.is/", "https://get.geojs.io/v1/ip/geo.json", "http://ip-api.com/json/")

CODES = {
    0: "clear skies", 1: "mostly clear skies", 2: "partly cloudy skies", 3: "overcast skies", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}
RAINY = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOWY = {71, 73, 75, 77, 85, 86}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class WeatherError(Exception):
    def __init__(self, message: str, offline: bool = False) -> None:
        super().__init__(message)
        self.offline = offline  # the service couldn't be reached (so a web search might still help)


@dataclass
class WeatherRequest:
    kind: str  # "now" | "temperature" | "rain" | "snow" | "wear" | "forecast"
    place: str = ""
    day: int = 0  # 0 today, 1 tomorrow, ... ; -1 = the weekend


@dataclass
class Place:
    name: str
    latitude: float
    longitude: float
    country: str = ""


# ----------------------------------------------------------------------------- understanding the question
_LEAD = r"^(?:please |can you |could you |would you |tell me |let me know |i want to know |do you know )*"
_WHEN = (r"(?P<when>today|tonight|right now|now|at the moment|currently|outside|out there|tomorrow|tomorrow morning|tomorrow night|"
         r"this (?:morning|afternoon|evening|weekend)|on the weekend|at the weekend|over the weekend|(?:on |this |next )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))")
_PLACE = r"(?:\s+(?:in|for|at|near|around)\s+(?P<place>[a-zà-ÿ0-9][a-zà-ÿ0-9 .,'-]{1,60}?))?"
_TAIL = r"(?:\s+(?:here|around here|where i am))?(?:\s+" + _WHEN + r")?" + _PLACE + r"(?:\s+" + _WHEN.replace("?P<when>", "?P<when2>") + r")?(?:\s+(?:please|for me))?[\s?.!]*$"
_PATTERNS = [
    ("temperature", re.compile(_LEAD + r"(?:(?:what(?:'s| is)|check|get(?: me)?|give me|show me)\s+)?(?:the\s+)?(?:current\s+|outside\s+)?(?:temperature|temp)(?:\s+(?:like|outside|out there))?" + _TAIL, re.I)),
    ("temperature", re.compile(_LEAD + r"(?:how\s+(?:hot|cold|warm|chilly)\s+(?:is it|will it be|is it going to be|it is)|what(?:'s| is)\s+it\s+like\s+outside|"
                               r"how many degrees is it|what temperature is it)" + _TAIL, re.I)),
    ("now", re.compile(_LEAD + r"(?:(?:what(?:'s| is| will be)|how(?:'s| is)|check|get(?: me)?|give me|show me)\s+)?(?:the\s+)?(?:weather|forecast|weather forecast)(?:\s+(?:like|looking like|looking|going to be like|going to be|be like))?" + _TAIL, re.I)),
    ("now", re.compile(_LEAD + r"(?:what(?:'s| is| will)\s+the\s+weather\s+(?:going to\s+)?(?:do|be)|weather(?:\s+report|\s+update|\s+forecast)?)" + _TAIL, re.I)),
    ("rain", re.compile(_LEAD + r"(?:is it|will it|is it going to|does it look like it(?:'s| is) going to|should i expect)\s+(?:rain|be rainy|pour|drizzle|shower|storm)(?:ing)?" + _TAIL, re.I)),
    ("rain", re.compile(_LEAD + r"(?:do i|will i|should i)\s+(?:need|take|bring)\s+(?:an?\s+)?(?:umbrella|raincoat|rain jacket)" + _TAIL, re.I)),
    ("snow", re.compile(_LEAD + r"(?:is it|will it|is it going to)\s+(?:snow|be snowy)(?:ing)?" + _TAIL, re.I)),
    ("wear", re.compile(_LEAD + r"(?:do i|will i|should i)\s+(?:need|wear|take|bring)\s+(?:an?\s+)?(?:jacket|coat|jumper|sweater|hoodie|sunscreen|sun cream|shorts)" + _TAIL, re.I)),
    ("wear", re.compile(_LEAD + r"what should i wear" + _TAIL, re.I)),
]
_NOT_PLACES = {"here", "my area", "my city", "my town", "town", "the city", "outside", "my location", "this area"}


def parse_weather(text: str) -> WeatherRequest | None:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if re.search(r"\b(?:cpu|processor|computer|pc|laptop|gpu|graphics card|core|cores|system)\b", t, re.I):
        return None  # "what's the CPU temperature" is about the computer
    for kind, pattern in _PATTERNS:
        m = pattern.match(t)
        if not m:
            continue
        place = (m.group("place") or "").strip(" .,")
        if place.lower() in _NOT_PLACES:
            place = ""
        when = (m.group("when") or m.group("when2") or "").lower()
        # a time mixed into the place: "for this weekend in Paris", "in Paris this weekend", "for tomorrow"
        lead = re.match(r"^(" + _WHEN.replace("?P<when>", "?:") + r")(?:\s+(?:in|for|at|near|around)\s+(.+))?$", place, re.I)
        if lead:
            when, place = lead.group(1).lower(), (lead.group(2) or "").strip(" .,")
        tail = re.search(r"\s+(" + _WHEN.replace("?P<when>", "?:") + r")$", place, re.I)
        if tail:
            when, place = when or tail.group(1).lower(), place[: tail.start()].strip(" .,")
        place = re.sub(r"\s+please$", "", place, flags=re.I)
        return WeatherRequest(kind, place, _day(when))
    return None


def _day(when: str) -> int:
    if not when or when in ("today", "tonight", "right now", "now", "at the moment", "currently", "outside", "out there") or when.startswith("this "):
        return -1 if "weekend" in when else 0
    if when.startswith("tomorrow"):
        return 1
    if "weekend" in when:
        return -1
    for i, name in enumerate(DAYS):
        if name in when:
            ahead = (i - datetime.now().weekday()) % 7
            return ahead + 7 if when.startswith("next ") and ahead == 0 else ahead
    return 0


# ----------------------------------------------------------------------------- the service
class Weather:
    def __init__(self, config, memory=None, client_factory=None) -> None:
        self.config = config
        self.memory = memory
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=True,
                                                                       headers={"User-Agent": "JARVIS desktop assistant"}))
        self._lock = threading.Lock()
        self._ip_place: tuple[float, Place] | None = None
        self._geo_cache: dict[str, Place] = {}
        self._forecast_cache: dict[tuple, tuple[float, dict]] = {}

    # -- where ----------------------------------------------------------------
    def home(self) -> str:
        """The user's town: the setting, else what they told JARVIS ("You live in Madrid")."""
        configured = (self.config.get("weather_location") or "").strip()
        if configured:
            return configured
        try:
            store = getattr(self.memory, "store", None) if self.memory is not None and self.memory.enabled else None
            for m in (store.all("fact") if store else []):
                found = re.match(r"^you (?:live|are living|stay) in ([A-Za-zÀ-ÿ .,'-]{2,50}?)\.?$", m.text, re.I)
                if found:
                    return found.group(1).strip()
        except Exception:
            log.debug("couldn't read the home town from memory", exc_info=True)
        return ""

    def locate(self, place: str = "") -> Place:
        name = place or self.home()
        if name:
            return self.geocode(name)
        return self.ip_location()

    def geocode(self, name: str) -> Place:
        """A town, city, village or postcode -> a place. "Bothell Washington", "Paris, TX", "Ashford Kent", "90210"."""
        key = name.lower().strip()
        if key in self._geo_cache:
            return self._geo_cache[key]
        found = None
        for city, hint in _candidates(name):
            data = self._get(GEOCODE_URL, {"name": city, "count": 50 if hint else 10, "language": "en", "format": "json"})
            found = _pick(data.get("results") or [], city, hint)
            if found:
                break
        if found is None:
            found = self._nominatim(name)
        if found is None:
            raise WeatherError(f"I couldn't find a place called {name}.")
        self._geo_cache[key] = found
        return found

    def _nominatim(self, name: str) -> Place | None:
        """OpenStreetMap's free geocoder: villages, neighbourhoods and postcodes the first one doesn't know."""
        try:
            rows = self._get(NOMINATIM_URL, {"q": name, "format": "jsonv2", "limit": 1, "addressdetails": 1})
        except WeatherError as exc:  # a fallback that can't be reached must not hide the real answer ("no such place")
            log.info("OpenStreetMap lookup for %r failed: %s", name, exc)
            return None
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        address = row.get("address") or {}
        label = (address.get("city") or address.get("town") or address.get("village") or address.get("hamlet")
                 or address.get("suburb") or str(row.get("display_name") or name).split(",")[0])
        return Place(str(label), float(row["lat"]), float(row["lon"]), str(address.get("country") or ""))

    def ip_location(self) -> Place:
        with self._lock:
            if self._ip_place and time.time() - self._ip_place[0] < 6 * 3600:
                return self._ip_place[1]
        for url in IP_LOOKUPS:
            try:
                d = self._get(url, None)
                lat = d.get("latitude", d.get("lat"))
                lon = d.get("longitude", d.get("lon"))
                if lat is None or lon is None:
                    continue
                place = Place(str(d.get("city") or "your area"), float(lat), float(lon), str(d.get("country") or ""))
                with self._lock:
                    self._ip_place = (time.time(), place)
                return place
            except Exception as exc:
                log.info("IP location via %s failed: %s", url, exc)
        raise WeatherError("I couldn't work out where you are. Tell me your town in Settings, under Weather, or ask about a place.", offline=True)

    # -- what -----------------------------------------------------------------
    def unit(self) -> str:
        chosen = self.config.get("temperature_unit", "auto")
        if chosen in ("celsius", "fahrenheit"):
            return chosen
        locale = (self.config.get("stt_language") or "en-US").upper()
        return "fahrenheit" if locale.endswith(("-US", "-LR", "-MM")) or locale in ("EN-US",) else "celsius"

    def forecast(self, place: Place) -> dict:
        key = (round(place.latitude, 2), round(place.longitude, 2), self.unit())
        cached = self._forecast_cache.get(key)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        data = self._get(FORECAST_URL, {
            "latitude": place.latitude, "longitude": place.longitude, "timezone": "auto", "forecast_days": 7,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,uv_index_max",
            "temperature_unit": self.unit(), "wind_speed_unit": "mph" if self.unit() == "fahrenheit" else "kmh",
        })
        if "current" not in data or "daily" not in data:
            raise WeatherError("the weather service sent back something I didn't understand.")
        self._forecast_cache[key] = (time.time(), data)
        return data

    def _get(self, url: str, params: dict | None) -> dict:
        try:
            with self._client_factory() as client:
                r = client.get(url, params=params)
                r.raise_for_status()
                return r.json()
        except httpx.HTTPStatusError as exc:  # reachable, but it said no (rate limit, bad request...)
            raise WeatherError(f"the weather service had a problem (HTTP {exc.response.status_code}).") from exc
        except httpx.HTTPError as exc:
            raise WeatherError("I couldn't reach the weather service. Check your internet connection.", offline=True) from exc
        except ValueError as exc:
            raise WeatherError("the weather service sent back something I didn't understand.") from exc

    # -- saying it -------------------------------------------------------------
    def answer(self, req: WeatherRequest, title: str = "sir") -> str:
        place = self.locate(req.place)
        data = self.forecast(place)
        return describe(req, place, data, self.unit(), title)


def _deg(value: float) -> str:
    return f"{round(value)} degree{'s' if round(value) not in (1, -1) else ''}"


def describe(req: WeatherRequest, place: Place, data: dict, unit: str, title: str = "sir") -> str:
    cur, daily = data["current"], data["daily"]
    where = place.name
    days = [date.fromisoformat(d) for d in daily["time"]]
    today = days[0]

    def day_index(offset: int) -> int:
        target = today + timedelta(days=offset)
        return days.index(target) if target in days else min(offset, len(days) - 1)

    def summary(i: int) -> str:
        code = int(daily["weather_code"][i])
        rain = daily.get("precipitation_probability_max", [None] * len(days))[i]
        text = f"{CODES.get(code, 'mixed weather')}, a high of {_deg(daily['temperature_2m_max'][i])} and a low of {_deg(daily['temperature_2m_min'][i])}"
        if rain is not None and rain >= 20:
            text += f", with a {round(rain)} percent chance of rain"
        return text

    def label(offset: int) -> str:
        return "today" if offset == 0 else "tomorrow" if offset == 1 else f"on {(today + timedelta(days=offset)).strftime('%A')}"

    if req.day == -1:  # the weekend
        sat = (5 - today.weekday()) % 7
        idx = [day_index(sat), day_index(sat + 1)]
        parts = [f"{days[i].strftime('%A')}: {summary(i)}" for i in idx if i < len(days)]
        return f"This weekend in {where}, {title}. " + ". ".join(parts) + "."
    if req.day > 0:
        i = day_index(req.day)
        if req.kind in ("rain", "snow"):
            return _will_it(req.kind, daily, i, where, label(req.day), title)
        return f"{label(req.day).capitalize()} in {where}: {summary(i)}, {title}."
    temp, feels = cur["temperature_2m"], cur.get("apparent_temperature", cur["temperature_2m"])
    code = int(cur.get("weather_code", daily["weather_code"][0]))
    feels_part = f", feels like {round(feels)}" if abs(feels - temp) >= 2 else ""
    if req.kind == "temperature":
        return (f"It's {_deg(temp)} in {where} right now{feels_part}, {title}. "
                f"Today's high is {round(daily['temperature_2m_max'][0])} and the low is {round(daily['temperature_2m_min'][0])}.")
    if req.kind in ("rain", "snow"):
        return _will_it(req.kind, daily, 0, where, "today", title, now_code=code)
    if req.kind == "wear":
        hi = daily["temperature_2m_max"][0]
        warm, cold = (75, 50) if unit == "fahrenheit" else (24, 10)
        rain = (daily.get("precipitation_probability_max") or [0])[0] or 0
        advice = "a warm coat" if hi < cold else "a light jacket" if hi < warm - 6 else "something light"
        extra = " and take an umbrella" if rain >= 40 else ""
        uv = (daily.get("uv_index_max") or [0])[0] or 0
        sun = " Sunscreen's a good idea too." if uv >= 6 else ""
        return f"It's {_deg(temp)} now with a high of {round(hi)} in {where}, so I'd go with {advice}{extra}, {title}.{sun}"
    return (f"Right now in {where} it's {_deg(temp)} with {CODES.get(code, 'mixed weather')}{feels_part}, {title}. "
            f"Today: {summary(0)}.")


def _will_it(kind: str, daily: dict, i: int, where: str, when: str, title: str, now_code: int | None = None) -> str:
    chance = (daily.get("precipitation_probability_max") or [None] * (i + 1))[i]
    code = int(daily["weather_code"][i])
    wet = (code in RAINY) if kind == "rain" else (code in SNOWY)
    if now_code is not None and ((kind == "rain" and now_code in RAINY) or (kind == "snow" and now_code in SNOWY)):
        return f"Yes, it's {CODES.get(now_code)} in {where} right now, {title}."
    word = "rain" if kind == "rain" else "snow"
    pct = f" ({round(chance)} percent chance)" if chance is not None else ""
    if wet or (chance or 0) >= 50:
        return f"Yes, {word} looks likely in {where} {when}{pct}, {title}." + (" Take an umbrella." if kind == "rain" else "")
    if (chance or 0) >= 25:
        return f"Possibly: there's a {round(chance)} percent chance of {word} in {where} {when}, {title}."
    return f"No {word} expected in {where} {when}{pct}, {title}."


# ----------------------------------------------------------------------------- the computer's own temperature
_PC_TEMP = re.compile(r"\b(?:(?:cpu|processor|computer|pc|laptop|gpu|system)(?:'s)?\s+(?:temp|temperature|heat)|how\s+hot\s+is\s+(?:my|the|this)\s+"
                      r"(?:cpu|processor|computer|pc|laptop|gpu|machine)|is\s+my\s+(?:computer|pc|laptop|cpu)\s+(?:overheating|too hot|running hot))\b", re.I)


def asks_pc_temperature(text: str) -> bool:
    return bool(_PC_TEMP.search(text or ""))


# ----------------------------------------------------------------------------- finding towns
US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado", "ct": "connecticut",
    "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan",
    "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin",
    "wy": "wyoming", "dc": "district of columbia",
}
COUNTRIES = {"uk": "united kingdom", "u.k.": "united kingdom", "england": "united kingdom", "scotland": "united kingdom",
             "wales": "united kingdom", "britain": "united kingdom", "us": "united states", "usa": "united states",
             "u.s.": "united states", "america": "united states", "uae": "united arab emirates"}
_STATE_WORDS = set(US_STATES.values())


def _expand(hint: str) -> str:
    h = hint.lower().strip(" .,")
    h = re.sub(r"^(?:the\s+)?(?:state\s+of\s+)", "", h)
    return US_STATES.get(h.replace(".", ""), COUNTRIES.get(h, h))


def _candidates(name: str) -> list[tuple[str, str]]:
    """Ways to read a spoken place: whole, or "town" + "state/country" hint ("Bothell Washington" -> Bothell in Washington)."""
    name = re.sub(r"\s+", " ", name.strip(" .,"))
    if "," in name:
        city, hint = name.split(",", 1)
        return [(city.strip(), _expand(hint)), (name.replace(",", ""), "")]
    words = name.split()
    out = [(name, "")]
    for k in range(len(words) - 1, 0, -1):
        out.append((" ".join(words[:k]), _expand(" ".join(words[k:]))))
    return out


def _pick(results: list[dict], city: str, hint: str) -> Place | None:
    if not results:
        return None
    def region(r: dict) -> str:
        return " ".join(str(r.get(k) or "") for k in ("country", "admin1", "admin2", "admin3", "country_code")).lower()
    if hint:
        h = hint.lower()
        words = [_expand(w) for w in h.split()]
        matching = [r for r in results if h in region(r) or all(w in region(r) for w in words)
                    or (len(h) == 2 and h == str(r.get("country_code", "")).lower())]
        if not matching:
            return None  # "Paris Texas" must not become Paris, France
        results = matching
    exact = [r for r in results if str(r.get("name", "")).lower() == city.lower()]
    best = max(exact or results, key=lambda r: int(r.get("population") or 0))
    return Place(str(best.get("name") or city), float(best["latitude"]), float(best["longitude"]), str(best.get("country") or ""))
