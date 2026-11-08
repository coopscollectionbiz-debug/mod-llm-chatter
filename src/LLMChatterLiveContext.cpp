/*
 * mod-llm-chatter - live, audience-scoped observations for reactive replies.
 */

#include "LLMChatterLiveContext.h"

#include "LLMChatterClaimPolicy.h"
#include "LLMChatterConfig.h"
#include "LLMChatterShared.h"

#include "CellImpl.h"
#include "Creature.h"
#include "GridNotifiers.h"
#include "GridNotifiersImpl.h"
#include "Map.h"
#include "Player.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <list>

namespace
{
struct VisibleRareCheck
{
    WorldObject const* focus;
    float radius;

    VisibleRareCheck(WorldObject const* object, float range) : focus(object), radius(range) {}

    WorldObject const& GetFocusObject() const { return *focus; }

    bool operator()(Creature* creature)
    {
        if (!creature || !creature->IsAlive() || IsLLMChatterInternalCreature(creature))
            return false;
        uint32 const rank = creature->GetCreatureTemplate()->rank;
        return (rank == CREATURE_ELITE_RARE || rank == CREATURE_ELITE_RAREELITE)
            && focus->IsWithinDistInMap(creature, radius);
    }
};

int32 PowerPct(Player* player, Powers power)
{
    uint32 const max = player->GetMaxPower(power);
    if (!max)
        return -1;
    return int32(std::lround(100.0 * player->GetPower(power) / max));
}

std::string WorldJson(Player* player)
{
    Map* map = player->GetMap();
    return Acore::StringFormat("\"m\":{},\"i\":{},\"p\":{},\"d\":{}", player->GetMapId(),
        map ? map->GetInstanceId() : 0, player->GetPhaseMask(), map ? uint32(map->GetDifficulty()) : 0);
}

std::string BotJson(Player* bot)
{
    Powers const power = bot->getPowerType();
    Unit* victim = bot->GetVictim();
    uint32 targetEntry = victim && victim->IsCreature() ? victim->ToCreature()->GetEntry() : 0;
    return Acore::StringFormat(
        "{{\"g\":{},\"a\":{},\"c\":{},\"hp\":{},\"pt\":{},\"pp\":{},{},\"x\":{},\"y\":{},"
        "\"role\":\"{}\",\"tgt\":{}}}",
        bot->GetGUID().GetCounter(), bot->IsAlive() ? 1 : 0, bot->IsInCombat() ? 1 : 0,
        int32(std::lround(bot->GetHealthPct())), uint32(power), PowerPct(bot, power), WorldJson(bot),
        int32(bot->GetPositionX()), int32(bot->GetPositionY()), JsonEscape(GetChatterBotRoleName(bot)), targetEntry);
}
} // namespace

bool MessageMentionsRare(std::string const& message)
{
    std::string lower(message);
    std::transform(lower.begin(), lower.end(), lower.begin(),
        [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    size_t pos = lower.find("rare");
    while (pos != std::string::npos)
    {
        bool const left = pos == 0 || !std::isalpha(static_cast<unsigned char>(lower[pos - 1]));
        size_t const after = pos + 4;
        bool const right = after >= lower.size() || !std::isalpha(static_cast<unsigned char>(lower[after]))
            || lower.compare(after, 1, "s") == 0;
        if (left && right)
            return true;
        pos = lower.find("rare", pos + 1);
    }
    return false;
}

std::string BuildReactiveLiveJson(Player* anchor, std::vector<Player*> const& bots, bool scanRares)
{
    if (!anchor || !sLLMChatterConfig || !sLLMChatterConfig->_liveContextEnable)
        return {};

    uint64 const nowMs = uint64(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count());
    std::string json = Acore::StringFormat("\"live\":{{\"t\":{},\"anchor\":{{{}}},\"bots\":[", nowMs,
        WorldJson(anchor));
    size_t count = 0;
    for (Player* bot : bots)
    {
        if (!bot || !bot->IsInWorld() || count >= 8)
            continue;
        json += (count++ ? "," : "") + BotJson(bot);
    }
    json += "]";

    if (scanRares && sLLMChatterConfig->_liveRareScanRadius > 0)
    {
        float const radius = float(sLLMChatterConfig->_liveRareScanRadius);
        std::list<Creature*> creatures;
        VisibleRareCheck check(anchor, radius);
        Acore::CreatureListSearcher<VisibleRareCheck> searcher(anchor, creatures, check);
        Cell::VisitObjects(anchor, searcher, radius);

        json += ",\"rares\":[";
        size_t rares = 0;
        for (Creature* creature : creatures)
        {
            if (rares >= 4)
                break;
            for (Player* bot : bots)
            {
                // A rare is only "seen" by a bot that can actually see it.
                if (!bot || !bot->IsInWorld() || bot->GetMap() != creature->GetMap()
                    || !bot->IsWithinDistInMap(creature, LLMChatterClaimPolicy::RARE_MAX_DISTANCE)
                    || !bot->CanSeeOrDetect(creature))
                    continue;
                json += Acore::StringFormat("{}{{\"g\":{},\"s\":{},\"e\":{},\"n\":\"{}\",\"yd\":{}}}",
                    rares++ ? "," : "", bot->GetGUID().GetCounter(), creature->GetSpawnId(),
                    creature->GetEntry(), JsonEscape(creature->GetName()),
                    int32(bot->GetDistance(creature)));
                break;
            }
        }
        json += "]";
    }
    return json + "}";
}

bool ReactiveClaimsHold(std::string const& claims, Player* bot, std::string& reason)
{
    using namespace LLMChatterClaimPolicy;
    if (claims.empty())
        return true;
    std::vector<Claim> parsed;
    if (!ParseClaims(claims, parsed))
    {
        reason = "malformed_live_claim";
        return false;
    }
    for (Claim const& claim : parsed)
    {
        if (!bot || claim.subject != bot->GetGUID().GetCounter())
        {
            reason = "live_claim_wrong_speaker";
            return false;
        }
        if (claim.kind == ClaimKind::RareVisible)
        {
            RareState rare;
            if (Creature* creature = FindCreatureBySpawnId(bot->GetMap(), claim.spawnId))
            {
                rare.found = true;
                rare.alive = creature->IsAlive();
                rare.entry = creature->GetEntry();
                rare.visibleToSpeaker = bot->CanSeeOrDetect(creature);
                rare.distance = bot->GetDistance(creature);
            }
            if (!RareClaimHolds(claim, rare))
            {
                reason = "stale_rare_claim";
                return false;
            }
            continue;
        }
        SpeakerState state;
        state.present = bot->IsInWorld();
        state.alive = bot->IsAlive();
        state.inCombat = bot->IsInCombat();
        state.powerType = claim.powerType;
        state.powerPct = claim.powerType < MAX_POWERS ? PowerPct(bot, Powers(claim.powerType)) : -1;
        if (bot->getPowerType() != Powers(claim.powerType))
            state.powerType = uint32(bot->getPowerType());
        if (!SpeakerClaimHolds(claim, state))
        {
            reason = "stale_live_claim";
            return false;
        }
    }
    return true;
}
