/*
 * mod-llm-chatter - delivery-time revalidation of volatile reply claims.
 *
 * A reactive reply generated from a live observation ("mana sec", "I can
 * see it by the road") records the observation it relied on. Immediately
 * before the line is sent, delivery re-reads the live state and drops the
 * line if the claim no longer holds. Header-only and free of game types so
 * the decision logic is unit tested standalone.
 *
 * Wire format (llm_chatter_messages.claim_json), produced by
 * tools/chatter_dialogue.py: claims separated by '|', each
 * "kind;key=value;..." with unsigned integer values, for example
 *   power_low;g=42;pt=0;th=35|rare;g=42;s=118811;e=61
 */
#ifndef MOD_LLM_CHATTER_CLAIM_POLICY_H
#define MOD_LLM_CHATTER_CLAIM_POLICY_H

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace LLMChatterClaimPolicy
{
enum class ClaimKind
{
    PowerLow,     // speaker said it is low on its power (mana/energy/...)
    PowerOk,      // speaker said its power is fine
    Alive,        // speaker said it is alive / ready to act
    Dead,         // speaker said it is dead
    OutOfCombat,  // speaker said it is not fighting
    InCombat,     // speaker said it is fighting
    RareVisible,  // speaker said it can see a specific live rare
    Unknown,
};

struct Claim
{
    ClaimKind kind = ClaimKind::Unknown;
    uint32_t subject = 0;    // speaking bot GUID (low)
    uint32_t powerType = 0;  // Powers enum value
    uint32_t threshold = 35; // percent boundary used when generating
    uint32_t spawnId = 0;    // creature spawn id for RareVisible
    uint32_t entry = 0;      // creature entry for RareVisible
};

// Snapshot of the speaking bot, read on the world thread at send time.
struct SpeakerState
{
    bool present = false;
    bool alive = false;
    bool inCombat = false;
    uint32_t powerType = 0;
    int32_t powerPct = -1; // -1: the speaker has no such power
};

// Snapshot of the claimed rare, resolved by spawn id at send time.
struct RareState
{
    bool found = false;
    bool alive = false;
    bool visibleToSpeaker = false;
    uint32_t entry = 0;
    float distance = 0.0f;
};

// Hysteresis keeps a line about "~35% mana" valid through small regen or
// spend between generation and delivery, while catching real reversals.
constexpr int32_t POWER_HYSTERESIS_PCT = 10;
constexpr float RARE_MAX_DISTANCE = 100.0f;

inline bool ParseUInt(std::string_view text, uint32_t& out)
{
    if (text.empty() || text.size() > 10)
        return false;
    uint64_t value = 0;
    for (char c : text)
    {
        if (c < '0' || c > '9')
            return false;
        value = value * 10 + uint64_t(c - '0');
    }
    if (value > 0xFFFFFFFFull)
        return false;
    out = uint32_t(value);
    return true;
}

inline ClaimKind ParseKind(std::string_view kind)
{
    if (kind == "power_low") return ClaimKind::PowerLow;
    if (kind == "power_ok") return ClaimKind::PowerOk;
    if (kind == "alive") return ClaimKind::Alive;
    if (kind == "dead") return ClaimKind::Dead;
    if (kind == "out_of_combat") return ClaimKind::OutOfCombat;
    if (kind == "in_combat") return ClaimKind::InCombat;
    if (kind == "rare") return ClaimKind::RareVisible;
    return ClaimKind::Unknown;
}

// Returns false when any segment is malformed: a corrupt claim fails closed.
inline bool ParseClaims(std::string_view text, std::vector<Claim>& out)
{
    out.clear();
    if (text.empty())
        return true;
    if (text.size() > 512)
        return false;
    size_t start = 0;
    while (start <= text.size())
    {
        size_t end = text.find('|', start);
        std::string_view segment = text.substr(start, end == std::string_view::npos ? std::string_view::npos : end - start);
        Claim claim;
        size_t field = 0;
        bool first = true;
        while (field <= segment.size())
        {
            size_t next = segment.find(';', field);
            std::string_view part = segment.substr(field, next == std::string_view::npos ? std::string_view::npos : next - field);
            if (first)
            {
                claim.kind = ParseKind(part);
                if (claim.kind == ClaimKind::Unknown)
                    return false;
                first = false;
            }
            else
            {
                size_t eq = part.find('=');
                if (eq == std::string_view::npos)
                    return false;
                std::string_view key = part.substr(0, eq);
                uint32_t value = 0;
                if (!ParseUInt(part.substr(eq + 1), value))
                    return false;
                if (key == "g") claim.subject = value;
                else if (key == "pt") claim.powerType = value;
                else if (key == "th") claim.threshold = value > 100 ? 100 : value;
                else if (key == "s") claim.spawnId = value;
                else if (key == "e") claim.entry = value;
                else return false;
            }
            if (next == std::string_view::npos)
                break;
            field = next + 1;
        }
        if (!claim.subject || (claim.kind == ClaimKind::RareVisible && (!claim.spawnId || !claim.entry)))
            return false;
        out.push_back(claim);
        if (out.size() > 4)
            return false;
        if (end == std::string_view::npos)
            break;
        start = end + 1;
    }
    return true;
}

// Speaker-side claims. The caller guarantees `state` belongs to the speaker.
inline bool SpeakerClaimHolds(Claim const& claim, SpeakerState const& state)
{
    if (!state.present)
        return false;
    switch (claim.kind)
    {
        case ClaimKind::PowerLow:
            return state.alive && state.powerType == claim.powerType && state.powerPct >= 0
                && state.powerPct < int32_t(claim.threshold) + POWER_HYSTERESIS_PCT;
        case ClaimKind::PowerOk:
            return state.alive && state.powerType == claim.powerType && state.powerPct >= 0
                && state.powerPct >= int32_t(claim.threshold) - POWER_HYSTERESIS_PCT;
        case ClaimKind::Alive:
            return state.alive;
        case ClaimKind::Dead:
            return !state.alive;
        case ClaimKind::OutOfCombat:
            return !state.inCombat;
        case ClaimKind::InCombat:
            return state.inCombat;
        default:
            return false;
    }
}

inline bool RareClaimHolds(Claim const& claim, RareState const& rare)
{
    return rare.found && rare.alive && rare.visibleToSpeaker && rare.entry == claim.entry
        && rare.distance <= RARE_MAX_DISTANCE;
}
} // namespace LLMChatterClaimPolicy

#endif
