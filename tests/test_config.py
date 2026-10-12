"""Settings: config.json parsing, the key store, and which TV a call means."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass

import pytest

from webos_mcp import keystore
from webos_mcp.config import (
    ALLOWED_KEYS,
    LEFT_OUT_KEYS,
    TvSettings,
    is_local_ipv4,
    load_settings,
    name_key,
    normalize_mac,
    upsert_tv,
)
from webos_mcp.errors import WebOsError
from webos_mcp.tvs import pick, pick_many


def write(home, data) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps(data))


def test_empty_config_is_fine(home):
    s = load_settings()
    assert s.tvs == () and s.problems == () and "netflix" in s.apps


def test_tvs_parse_with_defaults(home):
    write(home, {"tvs": [{"name": "Office", "host": "192.168.1.60", "macs": ["A8-23-FE-01-02-03"]}]})
    (tv,) = load_settings().tvs
    assert tv.macs == ("a8:23:fe:01:02:03",)
    assert (tv.max_volume, tv.volume_owner) == (40, "auto")


@pytest.mark.parametrize("host", ["8.8.8.8", "tv.example.com", "169.254.169.254", "", None, "fe80::1"])
def test_only_private_ipv4_hosts(home, host):
    write(home, {"tvs": [{"name": "Office", "host": host}]})
    s = load_settings()
    assert s.tvs == () and "not a private IPv4 address" in s.problems[0]


def test_local_networks():
    assert all(is_local_ipv4(h) for h in ("10.1.2.3", "172.16.0.9", "192.168.1.60", "127.0.0.3", "169.254.1.1"))
    assert not any(is_local_ipv4(h) for h in ("172.32.0.1", "100.64.0.1", "169.254.169.254", "1.1.1.1"))


def test_bad_values_become_problems_not_crashes(home):
    write(
        home,
        {
            "tvs": [{"name": "Office", "host": "192.168.1.60", "max_volume": 500, "volume_owner": "nobody"}],
            "apps": {"bad": 3},
            "wol_port": "nine",
        },
    )
    s = load_settings()
    assert s.tvs[0].max_volume == 40 and s.tvs[0].volume_owner == "auto"
    assert len(s.problems) == 4


def test_broken_json_is_a_problem(home):
    home.mkdir()
    (home / "config.json").write_text("{nope")
    assert "not valid JSON" in load_settings().problems[0]


def test_a_name_meaning_two_tvs_is_refused(home):
    write(
        home,
        {
            "tvs": [
                {"name": "Office", "host": "192.168.1.60"},
                {"name": "Den", "host": "192.168.1.61", "aliases": ["office"]},
            ]
        },
    )
    s = load_settings()
    assert [t.name for t in s.tvs] == ["Office"] and "also 'Office'" in s.problems[0]


def test_user_apps_extend_the_allow_list(home):
    write(home, {"apps": {"Crunchyroll": "crunchyroll.app"}})
    s = load_settings()
    assert s.resolve_app("crunchyroll") == ("crunchyroll", s.apps["crunchyroll"])
    assert s.resolve_app("youtube.leanback.v4")[0] == "youtube"  # by id
    assert s.resolve_app("Prime Video")[0] == "prime-video"  # by title
    assert s.resolve_app("tiktok") is None


def test_upsert_matches_by_uuid_and_keeps_hand_edits(home):
    upsert_tv({"name": "Office", "host": "192.168.1.60", "uuid": "u-1"})
    data = json.loads((home / "config.json").read_text())
    data["tvs"][0]["max_volume"] = 25
    (home / "config.json").write_text(json.dumps(data))
    upsert_tv({"name": "Office", "host": "192.168.1.77", "uuid": "u-1"})
    (tv,) = load_settings().tvs
    assert (tv.host, tv.max_volume) == ("192.168.1.77", 25)
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600


def test_keys_are_private_and_by_uuid(home):
    keystore.put("u-1", "0123456789abcdef0123456789abcdef")
    assert keystore.get("u-1") == "0123456789abcdef0123456789abcdef"
    assert keystore.get("u-2") is None and keystore.get(None) is None
    assert stat.S_IMODE(keystore.path().stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_key_allow_list_and_left_out_keys_dont_overlap():
    assert not ALLOWED_KEYS & set(LEFT_OUT_KEYS)
    assert {"POWER", "IN_START", "EZ_ADJUST", "MENU"} <= set(LEFT_OUT_KEYS)  # ASSUMPTION W-KEYS


def test_names_and_macs():
    assert name_key("Family Room") == name_key("family-room") == "familyroom"
    assert normalize_mac("A823FE010203") == "a8:23:fe:01:02:03"
    assert normalize_mac("nope") is None


# --- which TV ---------------------------------------------------------------
@dataclass
class Item:
    tv: TvSettings


TVS = [
    Item(TvSettings("Family Room", "192.168.1.50", aliases=("G1",))),
    Item(TvSettings("Office", "192.168.1.60", aliases=("C5",))),
]


def test_pick_one_by_name_alias_or_address():
    assert pick(TVS, "family room") is TVS[0]
    assert pick(TVS, "g1") is TVS[0]
    assert pick(TVS, "192.168.1.60") is TVS[1]
    assert pick(TVS[:1], None) is TVS[0]  # only one: no need to name it


def test_never_guess():
    with pytest.raises(WebOsError, match="Which TV\\? Several are configured: Family Room .*Office"):
        pick(TVS, None)
    with pytest.raises(WebOsError, match="No TV called 'Bedroom'"):
        pick(TVS, "Bedroom")
    with pytest.raises(WebOsError, match="only works for set_power"):
        pick(TVS, "all")
    with pytest.raises(WebOsError, match="No TVs are configured"):
        pick([], None)


def test_all():
    assert pick_many(TVS, "ALL") == TVS
    assert pick_many(TVS, "office") == [TVS[1]]
