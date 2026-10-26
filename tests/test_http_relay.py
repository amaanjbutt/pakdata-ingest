"""Relayed hosts (pta.gov.pk) go through the Cloudflare relay Worker (ops/relay/) only when it is configured."""
from ingestion import http_client


def test_relay_rewrites_only_listed_hosts_when_configured(monkeypatch):
    monkeypatch.setattr(http_client, "_RELAY", "https://relay.example.workers.dev")
    monkeypatch.setattr(http_client, "_RELAY_KEY", "k3y")
    monkeypatch.setattr(http_client, "_RELAY_HOSTS", ["pta.gov.pk"])

    url, headers = http_client.relayed("https://www.pta.gov.pk/category/telecom-indicators/165", {"Accept": "x"})
    assert url == ("https://relay.example.workers.dev/?url="
                   "https%3A%2F%2Fwww.pta.gov.pk%2Fcategory%2Ftelecom-indicators%2F165")
    assert headers == {"Accept": "x", "X-Relay-Key": "k3y"}

    # other hosts go direct, untouched
    assert http_client.relayed("https://www.ismo.gov.pk/", None) == ("https://www.ismo.gov.pk/", None)


def test_relay_off_without_key(monkeypatch):
    monkeypatch.setattr(http_client, "_RELAY", "https://relay.example.workers.dev")
    monkeypatch.setattr(http_client, "_RELAY_KEY", "")
    u = "https://www.pta.gov.pk/category/telecom-indicators/165"
    assert http_client.relayed(u) == (u, None)
