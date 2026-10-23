from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest

from chatbot.weather import WeatherUnavailable, current_temperature, requested_city


def test_requested_city_extracts_hyderabad():
    assert requested_city("what the temperature in Hyderabad") == "Hyderabad"
    assert requested_city("weather for New York right now") == "New York"
    assert requested_city("what is the weather?") is None


def test_current_temperature_uses_fixed_open_meteo_hosts_and_formats_response():
    responses = [
        {"results": [{"name": "Hyderabad", "admin1": "Telangana", "country": "India",
                      "latitude": 17.385, "longitude": 78.4867}]},
        {"current": {"temperature_2m": 29.5, "time": "2026-10-22T12:15"},
         "current_units": {"temperature_2m": "°C"}},
    ]
    with patch("chatbot.weather._get_json", side_effect=responses) as get_json:
        answer = current_temperature("Hyderabad")
    assert answer == "The current temperature in Hyderabad, Telangana, India is 29.5°C as of 2026-10-22T12:15."
    urls = [call.args[0] for call in get_json.call_args_list]
    assert urlparse(urls[0]).hostname == "geocoding-api.open-meteo.com"
    assert urlparse(urls[1]).hostname == "api.open-meteo.com"
    params = parse_qs(urlparse(urls[1]).query)
    assert params["latitude"] == ["17.385"]
    assert params["longitude"] == ["78.4867"]
    assert params["current"] == ["temperature_2m"]


@pytest.mark.parametrize("city", ["", "https://example.com", "127.0.0.1"])
def test_weather_rejects_empty_or_non_city_input(city):
    with pytest.raises(WeatherUnavailable):
        current_temperature(city)