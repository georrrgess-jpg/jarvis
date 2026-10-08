"""Weather and temperature: question understanding, location, Open-Meteo answers and failures (simulated service)."""

from datetime import date, timedelta

import httpx
from tests.test_assistant import make  # noqa: F401  (a fixture)
import pytest

from core.weather import Place, Weather, WeatherError, WeatherRequest, asks_pc_temperature, describe, parse_weather

TODAY = date.today()


def forecast_payload(temp=14.2, feels=11.0, code=2, rain=(10, 70, 0, 0, 0, 0, 0)):
    days = [(TODAY + timedelta(days=i)).isoformat() for i in range(7)]
    return {
        "current": {"temperature_2m": temp, "apparent_temperature": feels, "weather_code": code, "relative_humidity_2m": 60, "wind_speed_10m": 12},
        "daily": {"time": days, "weather_code": [2, 63, 0, 71, 1, 3, 2], "temperature_2m_max": [16, 12, 18, 2, 15, 14, 13],
                  "temperature_2m_min": [9, 7, 8, -3, 6, 5, 4], "precipitation_probability_max": list(rain), "uv_index_max": [3, 2, 7, 1, 3, 3, 3]},
    }


class Service:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url)
        if self.fail:
            raise httpx.ConnectError("offline", request=request)
        host = request.url.host
        if host == "geocoding-api.open-meteo.com":
            name = request.url.params["name"].lower()
            if name == "nowhereville":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json={"results": [{"name": name.title(), "latitude": 48.85, "longitude": 2.35, "country": "France"}]})
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=forecast_payload())
        if host == "ipwho.is":
            return httpx.Response(200, json={"city": "Leeds", "latitude": 53.8, "longitude": -1.55, "country": "United Kingdom"})
        return httpx.Response(404)


@pytest.fixture
def service():
    return Service()


@pytest.fixture
def weather(config, service):
    config.update({"stt_language": "en-GB"})
    return Weather(config, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(service.handler)))


@pytest.mark.parametrize("text, kind, place, day", [
    ("what's the temperature", "temperature", "", 0), ("check the temperature", "temperature", "", 0),
    ("how cold is it outside", "temperature", "", 0), ("what's the weather like in Paris", "now", "Paris", 0),
    ("what's the weather in New York tomorrow", "now", "New York", 1), ("is it going to rain tomorrow", "rain", "", 1),
    ("do I need an umbrella", "rain", "", 0), ("do I need a jacket", "wear", "", 0), ("weather in London", "now", "London", 0),
    ("what's the forecast for this weekend", "now", "", -1), ("what's the temperature in Tokyo right now", "temperature", "Tokyo", 0),
    ("what's the forecast for this weekend in Paris", "now", "Paris", -1), ("what's the weather in Paris this weekend", "now", "Paris", -1),
    ("what's the weather for tomorrow in Rome", "now", "Rome", 1), ("weather for New York tomorrow", "now", "New York", 1),
    ("is it going to rain in Tokyo tomorrow", "rain", "Tokyo", 1), ("what's the temperature in London", "temperature", "London", 0),
])
def test_understands_weather_questions(text, kind, place, day):
    req = parse_weather(text)
    assert req is not None and (req.kind, req.place, req.day) == (kind, place, day)


@pytest.mark.parametrize("text", ["what's the CPU temperature", "how hot is the sun", "the temperature of boiling water", "open the weather app",
                                  "write a poem about the weather"])
def test_leaves_other_questions_alone(text):
    assert parse_weather(text) is None


def test_pc_temperature_questions():
    assert asks_pc_temperature("what's my CPU temperature") and asks_pc_temperature("is my laptop overheating")
    assert not asks_pc_temperature("what's the temperature")


def test_current_temperature_uses_your_connection_location(weather, service):
    reply = weather.answer(WeatherRequest("temperature"), "sir")
    assert reply == "It's 14 degrees in Leeds right now, feels like 11, sir. Today's high is 16 and the low is 9."
    assert any(u.host == "ipwho.is" for u in service.calls)


def test_a_named_place_is_looked_up(weather, service):
    reply = weather.answer(WeatherRequest("now", "Paris"), "sir")
    assert reply.startswith("Right now in Paris it's 14 degrees with partly cloudy skies")
    assert not any(u.host == "ipwho.is" for u in service.calls)


def test_settings_and_memory_say_where_home_is(weather, service, config):
    config.update({"weather_location": "Madrid"})
    assert "in Madrid" in weather.answer(WeatherRequest("temperature"))

    class Store:
        def all(self, kind):
            return [type("M", (), {"text": "You live in Lisbon."})()]

    class Memory:
        enabled = True
        store = Store()

    config.update({"weather_location": ""})
    weather.memory = Memory()
    assert weather.home() == "Lisbon"


def test_rain_tomorrow_and_the_weekend(weather):
    reply = weather.answer(WeatherRequest("rain", day=1), "sir")
    assert reply.startswith("Yes, rain looks likely in Leeds tomorrow (70 percent chance)")
    assert weather.answer(WeatherRequest("rain"), "sir") == "No rain expected in Leeds today (10 percent chance), sir."
    weekend = weather.answer(WeatherRequest("now", day=-1), "sir")
    assert weekend.startswith("This weekend in Leeds, sir.") and "Saturday" in weekend and "Sunday" in weekend


def test_what_to_wear(weather):
    assert "light jacket" in weather.answer(WeatherRequest("wear"), "sir")


def test_units_follow_your_language(weather, config, service):
    assert weather.unit() == "celsius"
    config.update({"stt_language": "en-US"})
    assert weather.unit() == "fahrenheit"
    weather._forecast_cache.clear()
    weather.answer(WeatherRequest("temperature"))
    assert any(u.params.get("temperature_unit") == "fahrenheit" for u in service.calls if u.host == "api.open-meteo.com")
    config.update({"temperature_unit": "celsius"})
    assert weather.unit() == "celsius"


def test_failures_are_explained(config):
    offline = Weather(config, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(Service(fail=True).handler)))
    with pytest.raises(WeatherError, match="internet|where you are"):
        offline.answer(WeatherRequest("temperature"))
    good = Weather(config, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(Service().handler)))
    with pytest.raises(WeatherError, match="couldn't find a place called Nowhereville"):
        good.answer(WeatherRequest("now", "Nowhereville"))


def test_describe_handles_snow_and_rain_now():
    data = forecast_payload(code=63)
    assert describe(WeatherRequest("rain"), Place("Oslo", 0, 0), data, "celsius").startswith("Yes, it's rain in Oslo right now")


def test_through_the_assistant(make, monkeypatch):
    from tests.test_personas import ask

    assistant, events = make()
    assistant.weather._client_factory = lambda: httpx.Client(transport=httpx.MockTransport(Service().handler))
    reply = ask(assistant, events, "what's the temperature")
    assert reply.startswith("It's 14 degrees in Leeds right now")
    assert events.of("tool_activity")[-1]["label"] == "Checking the weather"
    import core.osctl as osctl

    monkeypatch.setattr(osctl, "cpu_temperature", lambda: 61.0)
    assert ask(assistant, events, "what's my CPU temperature").startswith("Your processor is at 61 degrees Celsius, which is perfectly healthy")
    monkeypatch.setattr(osctl, "cpu_temperature", lambda: None)
    assert "doesn't report its processor temperature" in ask(assistant, events, "how hot is my computer")


def test_works_without_the_language_model(make, mock_ollama):
    from tests.test_personas import ask

    assistant, events = make()
    assistant.weather._client_factory = lambda: httpx.Client(transport=httpx.MockTransport(Service().handler))
    before = len([1 for p, _ in mock_ollama.requests if p == "/api/chat"])
    ask(assistant, events, "is it going to rain tomorrow")
    assert len([1 for p, _ in mock_ollama.requests if p == "/api/chat"]) == before



# ----------------------------------------------------------------------------- towns
GEONAMES = {
    "paris": [{"name": "Paris", "latitude": 48.85, "longitude": 2.35, "country": "France", "country_code": "FR", "admin1": "Île-de-France", "population": 2138551},
              {"name": "Paris", "latitude": 33.66, "longitude": -95.55, "country": "United States", "country_code": "US", "admin1": "Texas", "population": 24171}],
    "bothell": [{"name": "Bothell", "latitude": 47.76, "longitude": -122.2, "country": "United States", "country_code": "US", "admin1": "Washington", "population": 48161}],
    "ashford": [{"name": "Ashford", "latitude": 51.15, "longitude": 0.87, "country": "United Kingdom", "country_code": "GB", "admin1": "England", "admin2": "Kent", "population": 74204},
                {"name": "Ashford", "latitude": 51.43, "longitude": -0.46, "country": "United Kingdom", "country_code": "GB", "admin1": "England", "admin2": "Surrey", "population": 27382}],
}


class Towns(Service):
    def __init__(self):
        super().__init__()
        self.osm = []

    def handler(self, request):
        self.calls.append(request.url)
        host = request.url.host
        if host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json={"results": GEONAMES.get(request.url.params["name"].lower(), [])})
        if host == "nominatim.openstreetmap.org":
            self.osm.append(request.url.params["q"])
            if request.url.params["q"] in ("90210", "Little Snoring"):
                return httpx.Response(200, json=[{"lat": "34.09", "lon": "-118.41", "display_name": "Beverly Hills, CA",
                                                  "address": {"city": "Beverly Hills" if request.url.params["q"] == "90210" else None,
                                                              "village": "Little Snoring", "country": "United States"}}])
            return httpx.Response(200, json=[])
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=forecast_payload())
        return httpx.Response(404)


@pytest.fixture
def towns(config):
    service = Towns()
    return Weather(config, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(service.handler))), service


@pytest.mark.parametrize("spoken, name, lat", [
    ("Bothell Washington", "Bothell", 47.76), ("Bothell, WA", "Bothell", 47.76), ("Paris", "Paris", 48.85),
    ("Paris Texas", "Paris", 33.66), ("Paris, TX", "Paris", 33.66), ("Ashford Kent", "Ashford", 51.15),
    ("Ashford Surrey England", "Ashford", 51.43), ("Ashford", "Ashford", 51.15),
])
def test_finds_towns_however_they_are_said(towns, spoken, name, lat):
    weather, service = towns
    place = weather.geocode(spoken)
    assert place.name == name and place.latitude == lat


def test_villages_and_postcodes_fall_back_to_openstreetmap(towns):
    weather, service = towns
    assert weather.geocode("90210").name == "Beverly Hills"
    assert weather.geocode("Little Snoring").name == "Little Snoring"
    assert service.osm == ["90210", "Little Snoring"]
    with pytest.raises(WeatherError, match="couldn't find a place called Atlantis"):
        weather.geocode("Atlantis")


def test_spoken_town_and_state_end_to_end(towns):
    weather, service = towns
    reply = weather.answer(parse_weather("what's the weather in Bothell Washington tomorrow"), "sir")
    assert reply.startswith("Tomorrow in Bothell:")
