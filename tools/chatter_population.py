"""Experimental population samplers; not connected to live bot identities."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path


_DATA_PATH = Path(__file__).resolve().parent / "data" / "player_population.json"


@lru_cache(maxsize=1)
def _load_population_data():
    return json.loads(_DATA_PATH.read_text(encoding="utf-8"))


def resolve_player_age_v2(name: str) -> int:
    """Stable survey-informed age, restricted to 18-65.

    Published age bands are adjusted to the 18-65 range using
    explicit within-band modeling assumptions.
    """
    data = _load_population_data()
    research = data["research"]["gaming_age_distribution"]
    bands = research["age_bands_percent"]

    # Assume uniform ages within the published 16-24 and 65+ bands.
    # Retain ages 18-24 (7 of 9 years) and age 65 (1 of an
    # assumed 65-74 range). The 65+ adjustment is especially
    # uncertain because the survey band has no upper age bound.
    weights = [
        (18, 24, bands["16-24"] * 7 / 9),
        (25, 34, bands["25-34"]),
        (35, 44, bands["35-44"]),
        (45, 54, bands["45-54"]),
        (55, 64, bands["55-64"]),
        (65, 65, bands["65+"] / 10),
    ]

    seed = str(name or "playerbot").strip().casefold()
    digest = hashlib.sha256(
        f"human-age-v2:{seed}".encode("utf-8")
    ).digest()

    total = sum(weight for _, _, weight in weights)
    selection = (
        int.from_bytes(digest[:8], "big") / (2 ** 64)
    ) * total

    for low, high, weight in weights:
        if selection < weight:
            span = high - low + 1
            return low + int.from_bytes(
                digest[8:16], "big"
            ) % span
        selection -= weight

    return 65

def resolve_player_geography_v2(name: str) -> dict:
    """Stable country-first fictional geography selection."""
    data = _load_population_data()
    geography = data["geography_v2"]
    countries = geography["countries"]

    seed = str(name or "playerbot").strip().casefold()
    digest = hashlib.sha256(
        f"human-geography-v2:{seed}".encode("utf-8")
    ).digest()

    total = sum(float(country["weight"]) for country in countries)
    selection = (
        int.from_bytes(digest[:8], "big") / (2 ** 64)
    ) * total

    selected = countries[-1]

    for country in countries:
        weight = float(country["weight"])
        if selection < weight:
            selected = country
            break
        selection -= weight

    indices = selected["profile_indices"]

    profile_index = indices[
        int.from_bytes(digest[8:16], "big") % len(indices)
    ]

    culture, _original_hometown = data["cultures"][profile_index]

    # Choose the hometown independently of the cultural profile.
    # A separate hash keeps the selection deterministic.
    cities = geography["hometowns"][selected["code"]]
    city_digest = hashlib.sha256(
        f"human-hometown-v2:{seed}".encode("utf-8")
    ).digest()

    hometown = cities[
        int.from_bytes(city_digest[:8], "big") % len(cities)
    ]

    return {
        "country_code": selected["code"],
        "country": selected["name"],
        "ethnicity": culture,
        "hometown": hometown,
        "location": hometown,
    }

def resolve_player_life_stage_v2(name: str, age: int = None) -> dict:
    """Experimental deterministic, age-aware career selection."""
    data = _load_population_data()

    if age is None:
        age = resolve_player_age_v2(name)

    if not 18 <= age <= 65:
        raise ValueError("Player age must be between 18 and 65")

    if age <= 24:
        career_group = "young"
    elif age <= 44:
        career_group = "adult"
    else:
        career_group = "older"

    careers = data["careers"][career_group]

    seed = str(name or "playerbot").strip().casefold()
    digest = hashlib.sha256(
        f"human-career-v2:{seed}".encode("utf-8")
    ).digest()

    career_entry = careers[
        int.from_bytes(digest[:8], "big") % len(careers)
    ]

    if len(career_entry) != 3:
        raise ValueError("Career entry must contain three fields")

    career, education, work_schedule = career_entry

    return {
        "age": age,
        "career_group": career_group,
        "career": career,
        "education": education,
        "work_schedule": work_schedule,
    }

def resolve_player_household_v2(name: str, age: int = None) -> dict:
    """Select a deterministic, age-aware fictional household."""
    data = _load_population_data()

    if age is None:
        age = resolve_player_age_v2(name)

    if type(age) is not int or not 18 <= age <= 65:
        raise ValueError("Player age must be an integer between 18 and 65")

    if age <= 24:
        age_group = "18-24"
    elif age <= 34:
        age_group = "25-34"
    elif age <= 44:
        age_group = "35-44"
    elif age <= 54:
        age_group = "45-54"
    else:
        age_group = "55-65"

    entries = data["households_v2"]["age_groups"][age_group]

    seed = str(name or "playerbot").strip().casefold()
    digest = hashlib.sha256(
        f"human-household-v2:{seed}".encode("utf-8")
    ).digest()

    total = sum(entry["weight"] for entry in entries)
    if total <= 0:
        raise ValueError("Household weights must be positive")

    selection = int.from_bytes(digest[:8], "big") % total

    selected = entries[-1]

    for entry in entries:
        weight = entry["weight"]
        if selection < weight:
            selected = entry
            break
        selection -= weight

    return {
        "age": age,
        "household_age_group": age_group,
        "household": selected["situation"],
    }

def resolve_player_background_v2(name: str) -> dict:
    """Build a stable experimental fictional player biography."""
    age = resolve_player_age_v2(name)
    geography = resolve_player_geography_v2(name)
    career = resolve_player_life_stage_v2(name, age=age)
    household = resolve_player_family_v2(name, age=age)
    interests = resolve_player_interests_v2(name, age=age)

    localized_career, localized_education = localize_player_education_v2(
        career["career"],
        career["education"],
        geography["country_code"],
    )

    return {
        "name": str(name or "playerbot").strip() or "playerbot",
        "age": age,
        "country_code": geography["country_code"],
        "country": geography["country"],
        "ethnicity": geography["ethnicity"],
        "hometown": geography["hometown"],
        "location": geography["location"],
        "career": localized_career,
        "education": localized_education,
        "work_schedule": career["work_schedule"],
        "household": household["household"],
        "household_source": household["household_source"],
        "relationship_status": household["relationship_status"],
        "children": household["children"],
        "hobbies": interests["hobbies"],
        "wow_history": interests["wow_history"],
        "wow_experience": interests["wow_experience"],
        "social_habits": interests["social_habits"],
        "career_group": career["career_group"],
        "household_age_group": household["household_age_group"],
        "population_version": "experimental-v2",
    }

def localize_player_education_v2(
    career: str,
    education: str,
    country_code: str,
) -> tuple:
    """Localize selected student terminology for fictional profiles."""

    if career != "community college student":
        return career, education

    equivalents = {
        "US": (
            "community college student",
            "currently attending community college",
        ),
        "CA": (
            "college student",
            "currently attending college",
        ),
        "GB": (
            "further education college student",
            "currently attending further education college",
        ),
        "AU": (
            "TAFE student",
            "currently attending technical and further education",
        ),
        "DE": (
            "university of applied sciences student",
            "currently studying at a university of applied sciences",
        ),
        "FR": (
            "higher vocational education student",
            "currently studying in higher vocational education",
        ),
    }

    return equivalents.get(
        country_code,
        (
            "technical college student",
            "currently attending a technical college",
        ),
    )

def resolve_player_interests_v2(name: str, age: int = None) -> dict:
    """Deterministic fictional hobbies, social habits, and WoW history."""
    data = _load_population_data()

    if age is None:
        age = resolve_player_age_v2(name)

    if type(age) is not int or not 18 <= age <= 65:
        raise ValueError("Player age must be an integer between 18 and 65")

    seed = str(name or "playerbot").strip().casefold()

    def choose(domain: str, options):
        if not options:
            raise ValueError(f"Empty population pool: {domain}")

        digest = hashlib.sha256(
            f"human-{domain}-v2:{seed}".encode("utf-8")
        ).digest()

        index = int.from_bytes(digest[:8], "big") % len(options)
        return options[index]

    hobbies = choose("hobbies", data["hobbies"])
    social_habits = choose("social", data["social_habits"])

    # These are provisional fictional modeling weights.
    # Younger players can still be experienced, but original-era
    # veteran histories are reserved for plausible ages.
    if age <= 24:
        experience_weights = (
            ("new", 40),
            ("intermediate", 60),
        )
    elif age <= 34:
        experience_weights = (
            ("new", 20),
            ("intermediate", 55),
            ("veteran", 25),
        )
    else:
        experience_weights = (
            ("new", 15),
            ("intermediate", 45),
            ("veteran", 40),
        )

    digest = hashlib.sha256(
        f"human-wow-experience-v2:{seed}".encode("utf-8")
    ).digest()

    total = sum(weight for _, weight in experience_weights)
    selection = int.from_bytes(digest[:8], "big") % total

    experience = experience_weights[-1][0]

    for category, weight in experience_weights:
        if selection < weight:
            experience = category
            break
        selection -= weight

    histories = list(data["wow_history"][experience])

    # Original vanilla/TBC/Wrath launch histories require an age
    # consistent with having played during those expansions.
    # Current reference year: 2026.
    minimum_ages = {
        "played WoW since vanilla and enjoys raiding": 28,
        "started during Burning Crusade and likes dungeons": 26,
        "started during Wrath and enjoys group content": 24,
    }

    histories = [
        history for history in histories
        if age >= minimum_ages.get(history, 18)
    ]

    if not histories:
        histories = list(data["wow_history"]["intermediate"])
        experience = "intermediate"

    wow_history = choose("wow-history", histories)

    return {
        "hobbies": hobbies,
        "social_habits": social_habits,
        "wow_experience": experience,
        "wow_history": wow_history,
    }

def resolve_player_family_v2(name: str, age: int = None) -> dict:
    """Turn a weighted household outcome into consistent family facts."""
    selected = resolve_player_household_v2(name, age=age)
    situation = selected["household"]

    mappings = {
        "lives with parents or relatives":
            ("lives with parents or relatives", "unspecified", "unspecified"),
        "shares an apartment with roommates":
            ("shares an apartment with roommates", "unspecified", "unspecified"),
        "lives alone":
            ("lives alone", "unspecified", "unspecified"),
        "lives with a partner":
            ("lives with a partner", "partnered", "unspecified"),
        "married or partnered with a young child":
            ("lives with a partner and young child", "partnered", "has a young child"),
        "shares a home with roommates":
            ("shares a home with roommates", "unspecified", "unspecified"),
        "married or partnered with children":
            ("lives with a partner and children", "partnered", "has children at home"),
        "single parent":
            ("lives with children", "single", "has children at home"),
        "lives with relatives":
            ("lives with relatives", "unspecified", "unspecified"),
        "shares a home with relatives":
            ("shares a home with relatives", "unspecified", "unspecified"),
        "married or partnered with teenage children":
            ("lives with a partner and teenage children", "partnered", "has teenage children at home"),
        "lives with a spouse or partner":
            ("lives with a spouse or partner", "partnered", "unspecified"),
        "lives with a spouse and an adult child":
            ("lives with a spouse and adult child", "married", "has an adult child at home"),
    }

    if situation == "has adult children who live elsewhere":
        seed = str(name or "playerbot").strip().casefold()
        digest = hashlib.sha256(
            f"human-empty-nest-v2:{seed}".encode("utf-8")
        ).digest()

        if int.from_bytes(digest[:8], "big") % 2:
            household = "lives alone"
            relationship = "unspecified"
        else:
            household = "lives with a spouse or partner"
            relationship = "partnered"

        children = "has adult children living elsewhere"
    else:
        if situation not in mappings:
            raise ValueError(f"Unknown household situation: {situation}")

        household, relationship, children = mappings[situation]

    return {
        "age": selected["age"],
        "household_age_group": selected["household_age_group"],
        "household_source": situation,
        "household": household,
        "relationship_status": relationship,
        "children": children,
    }
