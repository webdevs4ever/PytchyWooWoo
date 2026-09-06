"""Weather ingestion via the National Weather Service API.

NWS is free, official, and needs no API key — only a User-Agent identifying the
caller. Fetching a forecast is a two-step handshake: resolve lat/lon to a grid
point, then read that grid's hourly forecast.

Coverage is US-only, which is fine for NFL venues except international games.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

import config
from models import Game, WeatherCondition


class WeatherUnavailable(Exception):
    """Raised when NWS cannot be reached or has no forecast for the venue."""


def _headers() -> dict[str, str]:
    return {"User-Agent": config.NWS_USER_AGENT, "Accept": "application/geo+json"}


def _parse_wind_mph(raw: str | None) -> float:
    """NWS reports wind as text: '10 mph' or '5 to 15 mph'. Take the high end."""
    if not raw:
        return 0.0
    numbers = [int(tok) for tok in raw.replace("to", " ").split() if tok.isdigit()]
    return float(max(numbers)) if numbers else 0.0


def _hourly_forecast_url(latitude: float, longitude: float) -> str:
    resp = requests.get(
        f"{config.NWS_BASE_URL}/points/{latitude:.4f},{longitude:.4f}",
        headers=_headers(),
        timeout=config.REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    url = resp.json().get("properties", {}).get("forecastHourly")
    if not url:
        raise WeatherUnavailable(f"No hourly forecast grid for {latitude},{longitude}")
    return url


def _period_at(periods: list[dict], target: datetime) -> dict:
    """Pick the forecast period covering `target`, or the nearest one to it.

    NWS only publishes ~156 hours ahead, so a kickoff further out than that
    falls back to the last available period rather than failing outright.
    """
    if not periods:
        raise WeatherUnavailable("Forecast contained no periods")

    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    def start_of(period: dict) -> datetime:
        return datetime.fromisoformat(period["startTime"])

    for period in periods:
        if start_of(period) <= target < datetime.fromisoformat(period["endTime"]):
            return period

    return min(periods, key=lambda p: abs((start_of(p) - target).total_seconds()))


def fetch_forecast(game: Game) -> WeatherCondition:
    """Return the forecast at kickoff for `game`'s venue.

    Dome games skip the network call entirely — the roof makes the forecast
    irrelevant, and it saves a request against a rate-limited API.
    """
    if game.venue.is_dome:
        return WeatherCondition.indoor()

    try:
        url = _hourly_forecast_url(game.venue.latitude, game.venue.longitude)
        resp = requests.get(
            url, headers=_headers(), timeout=config.REQUEST_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        periods = resp.json().get("properties", {}).get("periods", [])
    except requests.RequestException as exc:
        raise WeatherUnavailable(f"NWS request failed: {exc}") from exc

    period = _period_at(periods, game.kickoff)

    temperature = float(period.get("temperature", 60))
    if period.get("temperatureUnit") == "C":
        temperature = temperature * 9 / 5 + 32

    precip = (period.get("probabilityOfPrecipitation") or {}).get("value")

    return WeatherCondition(
        temperature_f=temperature,
        wind_mph=_parse_wind_mph(period.get("windSpeed")),
        precipitation_chance=(precip or 0) / 100.0,
        description=period.get("shortForecast", "Unknown"),
    )
