"""Small fixed-provider current-weather lookup for natural-language chat."""

import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class WeatherUnavailable(RuntimeError):
    pass


def requested_city(question: str) -> str | None:
    match = re.search(r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z .'-]{1,60}?)(?=\s+(?:right now|currently|today|now|tomorrow)\b|[?.!,]|$)",
                      question, re.I)
    if not match:
        return None
    city = " ".join(match.group(1).split()).strip(" .'-")
    return city if re.search(r"[A-Za-z]", city) else None


def _get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "InsightDock/1.0", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise WeatherUnavailable("Weather service is unavailable.")
            payload = response.read(256_000)
        data = json.loads(payload)
    except WeatherUnavailable:
        raise
    except Exception as exc:
        raise WeatherUnavailable("Weather service is unavailable.") from exc
    if not isinstance(data, dict):
        raise WeatherUnavailable("Weather service returned an invalid response.")
    return data


def current_temperature(city: str) -> str:
    city = " ".join(city.split())[:60]
    if not city or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,59}", city):
        raise WeatherUnavailable("Please provide a city name in English.")
    query = urlencode({"name": city, "count": 1, "language": "en", "format": "json"})
    places = _get_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}").get("results")
    if not isinstance(places, list) or not places:
        raise WeatherUnavailable("I couldn't find that city. Please try another city name.")
    place = places[0]
    try:
        latitude, longitude = float(place["latitude"]), float(place["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherUnavailable("Weather service did not return a location.") from exc
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise WeatherUnavailable("Weather service did not return a valid location.")
    params = urlencode({"latitude": latitude, "longitude": longitude,
                        "current": "temperature_2m", "timezone": "auto"})
    weather = _get_json(f"https://api.open-meteo.com/v1/forecast?{params}")
    current = weather.get("current")
    if not isinstance(current, dict):
        raise WeatherUnavailable("Current weather is unavailable for that city.")
    try:
        temperature = float(current["temperature_2m"])
        unit = weather.get("current_units", {}).get("temperature_2m", "°C")
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherUnavailable("Current temperature is unavailable for that city.") from exc
    if not (-100 <= temperature <= 70):
        raise WeatherUnavailable("Weather service returned an invalid temperature.")
    name = str(place.get("name", city))[:80]
    region = str(place.get("admin1", ""))[:80]
    country = str(place.get("country", ""))[:80]
    location = ", ".join(part for part in (name, region, country) if part)
    observed_at = str(current.get("time", ""))[:40]
    timestamp = f" as of {observed_at}" if observed_at else ""
    return f"The current temperature in {location} is {temperature:g}{unit}{timestamp}."
