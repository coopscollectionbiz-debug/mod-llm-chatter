/*
 * mod-llm-chatter - proximity chatter domain
 */

#include "LLMChatterProximity.h"

#include "LLMChatterConfig.h"
#include "LLMChatterShared.h"

#include "CellImpl.h"
#include "Creature.h"
#include "DBCStores.h"
#include "GridNotifiers.h"
#include "GridNotifiersImpl.h"
#include "Group.h"
#include "Item.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "ScriptedCreature.h"
#include "Map.h"
#include "World.h"
#include "WorldSession.h"
#include "WorldSessionMgr.h"

#include <algorithm>
#include <cctype>
#include <ctime>
#include <list>
#include <map>
#include <random>
#include <string>
#include <vector>

namespace
{
struct NearbyCreatureCheck
{
    WorldObject const* _obj;
    float _range;

    NearbyCreatureCheck(
        WorldObject const* obj, float range)
        : _obj(obj), _range(range) {}

    WorldObject const& GetFocusObject() const
    {
        return *_obj;
    }

    bool operator()(Unit* unit)
    {
        if (!unit || !unit->IsAlive())
            return false;
        if (!unit->ToCreature())
            return false;
        return _obj->IsWithinDistInMap(unit, _range);
    }
};

struct NearbyBotCheck
{
    WorldObject const* _obj;
    float _range;

    NearbyBotCheck(
        WorldObject const* obj, float range)
        : _obj(obj), _range(range) {}

    bool operator()(Player* other)
    {
        return other && other->IsInWorld()
            && _obj->IsWithinDistInMap(other, _range);
    }
};

struct ProximityCandidate
{
    bool isNPC = false;
    Player* bot = nullptr;
    Creature* npc = nullptr;
    uint32 id = 0;
    uint32 entry = 0;
    std::string name;
    std::string role;
    std::string className;
    std::string raceName;
    std::string subName;
};

struct ProximityParticipant
{
    uint32 id = 0;
    bool isNPC = false;
    std::string name;
};

struct ProximityScene
{
    uint32 sceneId = 0;
    uint32 playerGuid = 0;
    uint32 zoneId = 0;
    uint32 mapId = 0;
    std::vector<ProximityParticipant> participants;
    uint32 lastSpeakerId = 0;
    bool lastSpeakerIsNPC = false;
    std::string lastSpeakerName;
    std::string lastMessage;
    time_t lastActivity = 0;
    uint8 replyCount = 0;
    bool pendingReply = false;
    bool replyEligible = false;

    bool IsExpired() const
    {
        uint32 expiry =
            sLLMChatterConfig
                ? sLLMChatterConfig
                      ->_proxChatterReplyWindowSeconds
                : 30;
        return time(nullptr) - lastActivity
            > static_cast<time_t>(expiry);
    }
};

struct ProximityChatHold
{
    uint32 playerGuid = 0;
    time_t lastActivity = 0;
};

static std::map<std::string, time_t> _entityCooldowns;
static std::map<std::string, std::pair<time_t, uint32>>
    _zoneFatigue;
static std::map<uint32, ProximityScene> _activeScenes;
static std::map<uint32, std::vector<uint32>> _playerScenes;
static std::map<uint32, ProximityChatHold> _proximityChatHolds;
static std::mt19937 _rng(std::random_device{}());

bool IsSameGroup(Player* left, Group* group)
{
    if (!left || !group)
        return false;

    Group* botGroup = left->GetGroup();
    if (!botGroup)
        return false;

    return botGroup->GetGUID() == group->GetGUID();
}

bool IsEligibleProximityBot(
    Player* player, Player* bot, float radius)
{
    if (!player || !bot)
        return false;
    if (!IsPlayerBot(bot))
        return false;
    if (!bot->IsInWorld() || !bot->IsAlive())
        return false;
    if (bot->IsInCombat() || bot->IsMounted()
        || bot->IsFlying())
        return false;

    // Ordinary Playerbot ground travel commonly uses
    // POINT_MOTION_TYPE (including CityLife). That movement
    // must not make a nearby bot deaf to player /say.
    //
    // Keep rejecting genuinely unsafe states here:
    // transport/flight/teleport, controlled motion,
    // waypoint/escort movement, etc. Only ordinary point
    // travel is exempted for Playerbot proximity listening.
    MotionMaster* motion = bot->GetMotionMaster();
    if (!motion)
        return false;

    MovementGeneratorType currentMotion =
        motion->GetCurrentMovementGeneratorType();

    MovementGeneratorType activeMotion =
        motion->GetMotionSlotType(MOTION_SLOT_ACTIVE);

    bool hasPointTravel =
        currentMotion == POINT_MOTION_TYPE
        || activeMotion == POINT_MOTION_TYPE;

    // POINT_MOTION_TYPE is ordinary Playerbot ground travel,
    // including CityLife, but it must not mask some OTHER
    // genuinely unsafe state.
    bool hasUnsafeNonPointMotion =
        currentMotion == WAYPOINT_MOTION_TYPE
        || currentMotion == FLIGHT_MOTION_TYPE
        || currentMotion == ESCORT_MOTION_TYPE
        || activeMotion == WAYPOINT_MOTION_TYPE
        || activeMotion == FLIGHT_MOTION_TYPE
        || activeMotion == ESCORT_MOTION_TYPE;

    bool hasUnsafeControlledMotion =
        motion->GetMotionSlotType(MOTION_SLOT_CONTROLLED)
            != NULL_MOTION_TYPE;

    bool hasUnsafeTransportState =
        bot->IsInFlight()
        || bot->GetTransport()
        || bot->HasUnitMovementFlag(
            MOVEMENTFLAG_ONTRANSPORT)
        || bot->IsBeingTeleported();

    if (
        hasUnsafeNonPointMotion
        || hasUnsafeControlledMotion
        || hasUnsafeTransportState
    )
        return false;

    // The shared facing helper intentionally classifies
    // POINT_MOTION_TYPE as unsafe because a moving unit should
    // not be forcibly re-faced. For Playerbot /say listening,
    // ordinary point travel is the one allowed exception.
    if (HasUnsafeChatterFacingMotion(bot)
        && !hasPointTravel)
        return false;

    if (bot->GetMapId() != player->GetMapId())
        return false;
    if (!player->IsWithinDistInMap(bot, radius))
        return false;

    WorldSession* session = bot->GetSession();
    if (session && session->PlayerLoading())
        return false;

    return true;
}

bool IsEligibleProximityNPC(
    Player* player, Creature* cr, float radius)
{
    if (!player || !cr || !cr->IsAlive())
        return false;
    if (!player->IsWithinDistInMap(cr, radius))
        return false;
    if (cr->IsPet() || cr->IsTotem()
        || cr->IsGuardian())
        return false;
    if (cr->IsPlayer() || cr->IsInCombat())
        return false;
    if (HasUnsafeChatterFacingMotion(cr))
        return false;
    if (cr->IsHostileTo(player))
        return false;

    CreatureTemplate const* tmpl =
        cr->GetCreatureTemplate();
    if (!tmpl)
        return false;
    if (cr->GetName().empty())
        return false;

    switch (tmpl->type)
    {
        case CREATURE_TYPE_CRITTER:
        case CREATURE_TYPE_BEAST:
        case CREATURE_TYPE_MECHANICAL:
        case CREATURE_TYPE_ELEMENTAL:
        case CREATURE_TYPE_GAS_CLOUD:
        case CREATURE_TYPE_NON_COMBAT_PET:
        case CREATURE_TYPE_TOTEM:
            return false;
        default:
            break;
    }

    if (cr->IsGuard())
        return true;

    uint32 npcFlags =
        cr->GetUInt32Value(UNIT_NPC_FLAGS);
    if (npcFlags
        & (UNIT_NPC_FLAG_VENDOR
            | UNIT_NPC_FLAG_VENDOR_AMMO
            | UNIT_NPC_FLAG_VENDOR_FOOD
            | UNIT_NPC_FLAG_VENDOR_POISON
            | UNIT_NPC_FLAG_VENDOR_REAGENT
            | UNIT_NPC_FLAG_TRAINER
            | UNIT_NPC_FLAG_TRAINER_CLASS
            | UNIT_NPC_FLAG_TRAINER_PROFESSION
            | UNIT_NPC_FLAG_INNKEEPER
            | UNIT_NPC_FLAG_FLIGHTMASTER
            | UNIT_NPC_FLAG_QUESTGIVER))
        return true;

    return tmpl->type == CREATURE_TYPE_HUMANOID;
}

WorldObject* ResolveParticipantObject(
    Player* player,
    ProximityParticipant const& participant)
{
    if (!player || !player->IsInWorld())
        return nullptr;

    if (participant.isNPC)
        return FindCreatureBySpawnId(
            player->GetMap(), participant.id);

    ObjectGuid guid =
        ObjectGuid::Create<HighGuid::Player>(
            participant.id);
    Player* bot = ObjectAccessor::FindPlayer(guid);
    if (!bot || !bot->IsInWorld())
        return nullptr;
    if (bot->GetMapId() != player->GetMapId())
        return nullptr;
    return bot;
}

WorldObject* GetCandidateObject(
    ProximityCandidate const& candidate)
{
    if (candidate.isNPC)
        return candidate.npc;
    return candidate.bot;
}

bool SameCandidate(
    ProximityCandidate const& left,
    ProximityCandidate const& right)
{
    return left.id == right.id
        && left.isNPC == right.isNPC;
}

std::string ToLowerAscii(std::string value)
{
    std::transform(
        value.begin(), value.end(), value.begin(),
        [](unsigned char c)
        {
            return static_cast<char>(
                std::tolower(c));
        });
    return value;
}

bool IsAsciiNameChar(char c)
{
    unsigned char uc =
        static_cast<unsigned char>(c);
    return std::isalnum(uc) != 0;
}

bool ContainsNameWithBoundary(
    std::string const& messageLower,
    std::string const& nameLower)
{
    if (messageLower.empty() || nameLower.empty())
        return false;

    size_t pos = messageLower.find(nameLower);
    while (pos != std::string::npos)
    {
        bool beforeOk =
            pos == 0
            || !IsAsciiNameChar(
                messageLower[pos - 1]);
        size_t end = pos + nameLower.size();
        bool afterOk =
            end >= messageLower.size()
            || !IsAsciiNameChar(
                messageLower[end]);
        if (beforeOk && afterOk)
            return true;

        pos = messageLower.find(
            nameLower, pos + 1);
    }

    return false;
}

std::string FirstNameToken(
    std::string const& name)
{
    size_t end = name.find_first_of(" \t\r\n");
    if (end == std::string::npos)
        return name;
    return name.substr(0, end);
}

ProximityCandidate const* FindSelectedCandidate(
    Player* player,
    std::vector<ProximityCandidate> const& candidates)
{
    if (!player)
        return nullptr;

    ObjectGuid selGuid =
        player->GetGuidValue(UNIT_FIELD_TARGET);
    if (!selGuid)
        return nullptr;

    for (auto const& c : candidates)
    {
        if (c.isNPC && c.npc
            && c.npc->GetGUID() == selGuid)
            return &c;
        if (!c.isNPC && c.bot
            && c.bot->GetGUID() == selGuid)
            return &c;
    }

    return nullptr;
}

ProximityCandidate const* FindNamedCandidate(
    Player* player,
    std::vector<ProximityCandidate> const& candidates,
    std::string const& message)
{
    if (!player || message.empty())
        return nullptr;

    std::string msgLower = ToLowerAscii(message);

    auto findBest = [&](
        bool firstTokenOnly) -> ProximityCandidate const*
    {
        ProximityCandidate const* best = nullptr;
        float bestDist = 1e9f;

        for (auto const& c : candidates)
        {
            std::string matchName =
                firstTokenOnly
                    ? FirstNameToken(c.name)
                    : c.name;
            if (matchName.size() < 3)
                continue;

            std::string nameLower =
                ToLowerAscii(matchName);
            if (!ContainsNameWithBoundary(
                    msgLower, nameLower))
                continue;

            WorldObject* obj =
                GetCandidateObject(c);
            if (!obj)
                continue;

            float dist =
                player->GetDistance(obj);
            if (!best || dist < bestDist)
            {
                best = &c;
                bestDist = dist;
            }
        }

        return best;
    };

    ProximityCandidate const* fullName =
        findBest(false);
    if (fullName)
        return fullName;

    return findBest(true);
}

void SetProximitySocialPauseUntil(
    Player* bot, time_t until)
{
    if (!bot || !IsPlayerBot(bot))
        return;

    PlayerbotAI* botAI = GET_PLAYERBOT_AI(bot);
    if (!botAI)
        return;

    botAI->SetSocialPauseUntil(until);
}

void ClearProximitySocialPause(Player* bot)
{
    SetProximitySocialPauseUntil(bot, 0);
}
void PauseProximityChatPointMotionFor(
    Player* bot, uint32 durationMs)
{
    if (!bot || !bot->IsInWorld() || !durationMs)
        return;

    MotionMaster* motion = bot->GetMotionMaster();
    if (!motion)
        return;

    // Only ordinary active point travel is safe to pause.
    // Combat/follow/escort/flight movement is never replaced.
    if (motion->GetMotionSlotType(MOTION_SLOT_ACTIVE)
        != POINT_MOTION_TYPE)
    {
        return;
    }

    bot->PauseMovement(durationMs, MOTION_SLOT_ACTIVE);
}
void PauseProximityChatPointMotion(Player* bot)
{
    if (!bot || !bot->IsInWorld())
        return;

    MotionMaster* motion = bot->GetMotionMaster();
    if (!motion)
        return;

    if (motion->GetMotionSlotType(MOTION_SLOT_ACTIVE)
        != POINT_MOTION_TYPE)
    {
        return;
    }

    // Pause(0) is an indefinite PointMovementGenerator stall.
    // Unit::PauseMovement also stops the current move spline,
    // while preserving the point generator and its destination.
    bot->PauseMovement(0, MOTION_SLOT_ACTIVE);
}

void ResumeProximityChatPointMotion(Player* bot)
{
    if (!bot || !bot->IsInWorld())
        return;

    MotionMaster* motion = bot->GetMotionMaster();
    if (!motion)
        return;

    // Only resume the movement class that chatter pauses.
    // If combat/follow/escort/etc. replaced the active generator,
    // leave that newer movement completely alone.
    if (motion->GetMotionSlotType(MOTION_SLOT_ACTIVE)
        != POINT_MOTION_TYPE)
    {
        return;
    }

    bot->ResumeMovement(0, MOTION_SLOT_ACTIVE);
}
bool IsValidProximityChatHold(
    Player* player, Player* bot)
{
    if (!player || !bot || player == bot)
        return false;
    if (!player->IsInWorld() || !player->IsAlive())
        return false;
    if (!bot->IsInWorld() || !bot->IsAlive())
        return false;
    if (!IsPlayerBot(bot))
        return false;
    if (bot->IsInCombat() || player->IsInCombat())
        return false;
    if (bot->IsMounted() || bot->IsFlying())
        return false;
    if (bot->GetMapId() != player->GetMapId())
        return false;
    if (!player->IsWithinDistInMap(bot, 40.0f))
        return false;
    if (bot->IsHostileTo(player)
        || player->IsHostileTo(bot))
        return false;

    // Party and raid members must continue following their
    // normal Playerbots progression even if they speak in /say.
    if (IsSameGroup(bot, player->GetGroup()))
        return false;

    return true;
}

void EvictExpiredScenes()
{
    for (auto it = _activeScenes.begin();
         it != _activeScenes.end();)
    {
        if (it->second.IsExpired())
        {
            auto pit =
                _playerScenes.find(
                    it->second.playerGuid);
            if (pit != _playerScenes.end())
            {
                auto& ids = pit->second;
                ids.erase(
                    std::remove(
                        ids.begin(), ids.end(),
                        it->first),
                    ids.end());
                if (ids.empty())
                    _playerScenes.erase(pit);
            }
            it = _activeScenes.erase(it);
        }
        else
            ++it;
    }
}

void AddSceneParticipant(
    ProximityScene& scene, uint32 id,
    bool isNPC, std::string const& name)
{
    for (ProximityParticipant const& participant :
         scene.participants)
    {
        if (participant.id == id
            && participant.isNPC == isNPC)
            return;
    }

    ProximityParticipant participant;
    participant.id = id;
    participant.isNPC = isNPC;
    participant.name = name;
    scene.participants.push_back(participant);
}

std::string BuildBotParticipantJson(Player* bot)
{
    return std::string("{")
        + "\"name\":\""
        + JsonEscape(bot->GetName()) + "\","
        + "\"is_npc\":false,"
        + "\"bot_guid\":"
        + std::to_string(
            bot->GetGUID().GetCounter())
        + ",\"class\":\""
        + JsonEscape(GetChatterClassName(bot->getClass()))
        + "\",\"race\":\""
        + JsonEscape(GetRaceName(bot->getRace()))
        + "\",\"role\":\"bot\"}";
}

std::string BuildNPCParticipantJson(Creature* cr)
{
    return std::string("{")
        + "\"name\":\""
        + JsonEscape(cr->GetName()) + "\","
        + "\"is_npc\":true,"
        + "\"npc_entry\":"
        + std::to_string(cr->GetEntry())
        + ",\"npc_spawn_id\":"
        + std::to_string(cr->GetSpawnId())
        + ",\"role\":\""
        + JsonEscape(GetCreatureRoleName(cr))
        + "\",\"sub_name\":\""
        + JsonEscape(
            cr->GetCreatureTemplate()->SubName)
        + "\"}";
}

std::string BuildParticipantJson(
    ProximityCandidate const& candidate)
{
    if (candidate.isNPC && candidate.npc)
        return BuildNPCParticipantJson(
            candidate.npc);

    if (candidate.bot)
        return BuildBotParticipantJson(
            candidate.bot);

    return "{}";
}

std::string BuildParticipantsJson(
    std::vector<ProximityCandidate> const& candidates)
{
    std::string json = "[";
    for (size_t i = 0; i < candidates.size(); ++i)
    {
        if (i > 0)
            json += ",";
        json += BuildParticipantJson(candidates[i]);
    }
    json += "]";
    return json;
}

std::string BuildActionCandidatesJson(
    Player* player,
    std::vector<ProximityCandidate> const& candidates)
{
    std::string json = "[";
    bool first = true;

    for (auto const& candidate : candidates)
    {
        if (candidate.isNPC || !candidate.bot)
            continue;

        if (!first)
            json += ",";

        first = false;

        json += std::string("{")
            + "\"bot_guid\":"
            + std::to_string(candidate.id)
            + ",\"name\":\""
            + JsonEscape(candidate.name)
            + "\",\"class\":\""
            + JsonEscape(candidate.className)
            + "\",\"distance\":"
            + std::to_string(
                player
                    ? player->GetDistance(candidate.bot)
                    : 0.0f)
            + "}";
    }

    json += "]";
    return json;
}

std::string GetAreaNameForLocale(uint32 areaId)
{
    AreaTableEntry const* area =
        sAreaTableStore.LookupEntry(areaId);
    if (!area)
        return "";

    uint8 locale = sWorld->GetDefaultDbcLocale();
    char const* name = area->area_name[locale];
    if (!name || !*name)
        name = area->area_name[LOCALE_enUS];
    return name ? name : "";
}

uint32 ComputeEffectiveChance(Player* player)
{
    uint32 chance =
        sLLMChatterConfig->_proxChatterChance;
    if (!player)
        return chance;

    uint32 windowSeconds = std::max<uint32>(
        sLLMChatterConfig->_proxChatterEntityCooldown,
        sLLMChatterConfig->_proxChatterScanInterval
            * 3);
    std::string key = std::to_string(
        player->GetGUID().GetCounter())
        + ":"
        + std::to_string(player->GetZoneId());
    time_t now = time(nullptr);
    auto& state = _zoneFatigue[key];

    if (state.first == 0
        || now - state.first
            > static_cast<time_t>(windowSeconds))
    {
        state.first = now;
        state.second = 0;
        return chance;
    }

    if (state.second
        <= sLLMChatterConfig
               ->_proxChatterZoneFatigueThreshold)
        return chance;

    uint32 overflow = state.second
        - sLLMChatterConfig
              ->_proxChatterZoneFatigueThreshold;
    uint32 decay = overflow
        * sLLMChatterConfig
              ->_proxChatterZoneFatigueDecay;
    return decay >= chance ? 0 : chance - decay;
}

void NoteZoneTrigger(Player* player)
{
    if (!player)
        return;

    std::string key = std::to_string(
        player->GetGUID().GetCounter())
        + ":"
        + std::to_string(player->GetZoneId());
    auto& state = _zoneFatigue[key];
    state.first = time(nullptr);
    ++state.second;
}

void EvictExpiredProximityCooldowns()
{
    time_t now = time(nullptr);
    time_t entityCutoff = static_cast<time_t>(
        sLLMChatterConfig
            ->_proxChatterEntityCooldown);
    uint32 zoneWindow = std::max<uint32>(
        sLLMChatterConfig
            ->_proxChatterEntityCooldown,
        sLLMChatterConfig
                ->_proxChatterScanInterval
            * 3);
    time_t zoneCutoff =
        static_cast<time_t>(zoneWindow);

    for (auto it = _entityCooldowns.begin();
         it != _entityCooldowns.end();)
    {
        if (now - it->second > entityCutoff)
            it = _entityCooldowns.erase(it);
        else
            ++it;
    }

    for (auto it = _zoneFatigue.begin();
         it != _zoneFatigue.end();)
    {
        if (now - it->second.first > zoneCutoff)
            it = _zoneFatigue.erase(it);
        else
            ++it;
    }
}

std::string GetEntityCooldownKey(
    ProximityCandidate const& candidate)
{
    return std::string(candidate.isNPC ? "npc:" : "bot:")
        + std::to_string(candidate.id);
}

void CollectNearbyCombatBotsForSocialAddress(
    Player* player, float radius,
    std::vector<ProximityCandidate>& out)
{
    if (!player)
        return;

    std::list<Player*> nearbyPlayers;
    NearbyBotCheck check(player, radius);
    Acore::PlayerListSearcher<NearbyBotCheck>
        searcher(player, nearbyPlayers, check);
    Cell::VisitObjects(player, searcher, radius);

    for (Player* bot : nearbyPlayers)
    {
        if (!bot || bot == player)
            continue;
        if (!IsPlayerBot(bot)
            || !bot->IsInWorld()
            || !bot->IsAlive()
            || !bot->IsInCombat())
        {
            continue;
        }

        if (bot->IsHostileTo(player)
            || player->IsHostileTo(bot))
        {
            continue;
        }

        // Stop-and-chat ownership never applies to the player's
        // own party/raid progression, so pending social pull
        // suppression follows the same rule.
        if (IsSameGroup(bot, player->GetGroup()))
            continue;

        ProximityCandidate candidate;
        candidate.bot = bot;
        candidate.id = bot->GetGUID().GetCounter();
        candidate.entry = 0;
        candidate.name = bot->GetName();
        candidate.className =
            GetChatterClassName(bot->getClass());
        candidate.raceName =
            GetRaceName(bot->getRace());
        out.push_back(candidate);
    }
}
void CollectNearbyBots(
    Player* player, float radius,
    std::vector<ProximityCandidate>& out)
{
    std::list<Player*> nearbyPlayers;
    NearbyBotCheck check(player, radius);
    Acore::PlayerListSearcher<NearbyBotCheck>
        searcher(player, nearbyPlayers, check);
    Cell::VisitObjects(player, searcher, radius);

    for (Player* bot : nearbyPlayers)
    {
        if (!IsEligibleProximityBot(
                player, bot, radius))
            continue;

        ProximityCandidate candidate;
        candidate.bot = bot;
        candidate.id =
            bot->GetGUID().GetCounter();
        candidate.entry = 0;
        candidate.name = bot->GetName();
        candidate.className =
            GetChatterClassName(bot->getClass());
        candidate.raceName =
            GetRaceName(bot->getRace());
        out.push_back(candidate);
    }
}

void CollectNearbyNPCs(
    Player* player, float radius,
    std::vector<ProximityCandidate>& out)
{
    std::list<Creature*> creatures;
    NearbyCreatureCheck check(player, radius);
    Acore::CreatureListSearcher<
        NearbyCreatureCheck>
        searcher(player, creatures, check);
    Cell::VisitObjects(player, searcher, radius);

    for (Creature* creature : creatures)
    {
        if (!IsEligibleProximityNPC(
                player, creature, radius))
            continue;

        ProximityCandidate candidate;
        candidate.isNPC = true;
        candidate.npc = creature;
        candidate.id = creature->GetSpawnId();
        candidate.entry = creature->GetEntry();
        candidate.name = creature->GetName();
        candidate.role =
            GetCreatureRoleName(creature);
        candidate.subName =
            creature->GetCreatureTemplate()->SubName;
        out.push_back(candidate);
    }
}

void DeduplicateCandidates(
    std::vector<ProximityCandidate>& candidates)
{
    std::map<std::string, size_t> chosen;
    for (size_t i = 0; i < candidates.size(); ++i)
    {
        std::string key =
            (candidates[i].isNPC ? "npc:" : "bot:")
            + std::to_string(candidates[i].id);
        chosen[key] = i;
    }

    std::vector<ProximityCandidate> deduped;
    deduped.reserve(chosen.size());
    for (auto const& pair : chosen)
        deduped.push_back(
            candidates[pair.second]);
    candidates.swap(deduped);
}

std::string BuildNearbyNamesJson(
    std::vector<ProximityCandidate> const& allCandidates,
    std::vector<ProximityCandidate> const& speakers)
{
    std::set<uint32> speakerIds;
    for (auto const& s : speakers)
        speakerIds.insert(s.id);

    std::string json = "[";
    size_t count = 0;
    for (auto const& c : allCandidates)
    {
        if (speakerIds.count(c.id))
            continue;
        if (count >= 4)
            break;
        if (count > 0)
            json += ",";
        json += "\"" + JsonEscape(c.name) + "\"";
        ++count;
    }
    json += "]";
    return json;
}

std::string BuildBaseEventJson(
    Player* player,
    std::vector<ProximityCandidate> const& speakers,
    std::vector<ProximityCandidate> const& allCandidates,
    bool playerAddressed,
    uint32 maxLines)
{
    std::string botStates = "{";
    bool firstBotState = true;

    for (auto const& speaker : speakers)
    {
        // NPCs do not have PlayerBot state.
        if (speaker.isNPC || !speaker.bot)
            continue;

        if (!firstBotState)
            botStates += ",";

        firstBotState = false;

        uint32 botGuid =
            speaker.bot->GetGUID().GetCounter();

        botStates += "\"" +
            std::to_string(botGuid) + "\":{";
        botStates += BuildBotStateJson(speaker.bot);
        botStates += "}";
    }

    botStates += "}";

    return std::string("{")
        + "\"player_guid\":"
        + std::to_string(
            player->GetGUID().GetCounter())
        + ",\"player_name\":\""
        + JsonEscape(player->GetName())
        + "\",\"zone_id\":"
        + std::to_string(player->GetZoneId())
        + ",\"zone_name\":\""
        + JsonEscape(
            GetAreaNameForLocale(
                player->GetZoneId()))
        + "\",\"subzone_name\":\""
        + JsonEscape(
            GetAreaNameForLocale(
                player->GetAreaId()))
        + "\",\"player_addressed\":"
        + std::string(
            playerAddressed ? "true" : "false")
        + ",\"nearby_names\":"
        + BuildNearbyNamesJson(
            allCandidates, speakers)
        + ",\"line_delay_seconds\":"
        + std::to_string(
            sLLMChatterConfig
                ->_proxChatterConversationLineDelay)
        + ",\"max_lines\":"
        + std::to_string(maxLines)
        + ",\"participants\":"
        + BuildParticipantsJson(speakers)
        + ",\"action_candidates\":"
        + BuildActionCandidatesJson(
            player, allCandidates)
        + ",\"bot_states\":"
        + botStates
        + "}";
}
void QueueProximityEvent(
    Player* player, char const* eventType,
    std::vector<ProximityCandidate> const& speakers,
    std::vector<ProximityCandidate> const& allCandidates,
    bool playerAddressed, uint32 maxLines)
{
    if (!player || speakers.empty())
        return;

    ProximityCandidate const& first = speakers[0];
    std::string cooldownKey =
        GetEntityCooldownKey(first);
    if (IsEventOnCooldown(
            _entityCooldowns,
            cooldownKey,
            sLLMChatterConfig
                ->_proxChatterEntityCooldown))
        return;

    std::string json = BuildBaseEventJson(
        player, speakers, allCandidates,
        playerAddressed, maxLines);
    std::string escaped = EscapeString(json);

    QueueChatterEvent(
        eventType,
        "player",
        player->GetZoneId(),
        player->GetMapId(),
        GetChatterEventPriority(eventType),
        cooldownKey,
        first.isNPC ? 0 : first.id,
        first.name,
        player->GetGUID().GetCounter(),
        player->GetName(),
        first.entry,
        escaped,
        GetReactionDelaySeconds(eventType),
        sLLMChatterConfig
            ->_eventExpirationSeconds,
        false);

    SetEventCooldown(_entityCooldowns, cooldownKey);
    NoteZoneTrigger(player);
}

void QueuePlayerSayProximityEvent(
    Player* player, char const* eventType,
    std::vector<ProximityCandidate> const& speakers,
    std::vector<ProximityCandidate> const& allCandidates,
    uint32 maxLines,
    std::string const& playerMessage,
    std::string const& addressedName)
{
    if (!player || speakers.empty())
        return;

    ProximityCandidate const& first = speakers[0];

    std::string json = BuildBaseEventJson(
        player, speakers, allCandidates,
        true, maxLines);

    // Inject player_message and optional
    // addressed_name before closing brace.
    std::string extra =
        ",\"player_message\":\""
        + JsonEscape(playerMessage) + "\"";
    if (!addressedName.empty())
        extra += ",\"addressed_name\":\""
            + JsonEscape(addressedName) + "\"";
    if (!json.empty() && json.back() == '}')
        json.insert(json.size() - 1, extra);

    std::string escaped = EscapeString(json);
    std::string cooldownKey =
        GetEntityCooldownKey(first);

    QueueChatterEvent(
        eventType,
        "player",
        player->GetZoneId(),
        player->GetMapId(),
        GetChatterEventPriority(eventType),
        cooldownKey,
        first.isNPC ? 0 : first.id,
        first.name,
        player->GetGUID().GetCounter(),
        player->GetName(),
        first.entry,
        escaped,
        GetReactionDelaySeconds(eventType),
        sLLMChatterConfig
            ->_eventExpirationSeconds,
        false);

    // Set cooldown on speakers (not checked here,
    // but prevents ambient scan from re-using them).
    for (auto const& s : speakers)
        SetEventCooldown(
            _entityCooldowns,
            GetEntityCooldownKey(s));
}

bool HasContextWord(
    std::string const& messageLower,
    std::string const& word)
{
    return ContainsNameWithBoundary(
        messageLower, word);
}

bool HasAnyContextWord(
    std::string const& messageLower,
    std::initializer_list<char const*> words)
{
    for (char const* word : words)
    {
        if (HasContextWord(messageLower, word))
            return true;
    }

    return false;
}

ProximityCandidate const*
FindUniquePartialNamedCandidate(
    std::vector<ProximityCandidate> const& candidates,
    std::string const& message)
{
    std::string messageLower = ToLowerAscii(message);
    std::vector<std::string> tokens;
    std::string token;

    auto flushToken = [&]()
    {
        if (token.size() >= 3)
            tokens.push_back(token);

        token.clear();
    };

    for (char ch : messageLower)
    {
        unsigned char uch =
            static_cast<unsigned char>(ch);

        if (std::isalnum(uch))
            token.push_back(ch);
        else
            flushToken();
    }

    flushToken();

    ProximityCandidate const* match = nullptr;

    for (auto const& candidate : candidates)
    {
        std::string nameLower = ToLowerAscii(
            FirstNameToken(candidate.name));

        bool candidateMatches = false;

        for (std::string const& candidateToken : tokens)
        {
            if (
                candidateToken.size() < nameLower.size()
                && nameLower.rfind(candidateToken, 0) == 0
            )
            {
                candidateMatches = true;
                break;
            }
        }

        if (!candidateMatches)
            continue;

        if (match)
            return nullptr;

        match = &candidate;
    }

    return match;
}

bool CandidateMatchesEquipmentContext(
    ProximityCandidate const& candidate,
    std::string const& messageLower)
{
    if (!candidate.bot)
        return false;

    bool wantsWeapon = HasAnyContextWord(
        messageLower, {"weapon", "weapons"});
    bool wantsSword = HasAnyContextWord(
        messageLower, {"sword", "swords"});
    bool wantsAxe = HasAnyContextWord(
        messageLower, {"axe", "axes"});
    bool wantsBow = HasAnyContextWord(
        messageLower, {"bow", "bows"});
    bool wantsGun = HasAnyContextWord(
        messageLower, {"gun", "guns"});
    bool wantsMace = HasAnyContextWord(
        messageLower, {"mace", "maces"});
    bool wantsPolearm = HasAnyContextWord(
        messageLower, {"polearm", "polearms"});
    bool wantsStaff = HasAnyContextWord(
        messageLower, {"staff", "staves"});
    bool wantsFist = HasAnyContextWord(
        messageLower, {"fist weapon", "fist weapons"});
    bool wantsDagger = HasAnyContextWord(
        messageLower, {"dagger", "daggers"});
    bool wantsThrown = HasAnyContextWord(
        messageLower, {"thrown weapon", "thrown weapons"});
    bool wantsCrossbow = HasAnyContextWord(
        messageLower, {"crossbow", "crossbows"});
    bool wantsWand = HasAnyContextWord(
        messageLower, {"wand", "wands"});
    bool wantsFishingPole = HasAnyContextWord(
        messageLower, {"fishing pole", "fishing poles"});
    bool wantsShield = HasAnyContextWord(
        messageLower, {"shield", "shields"});

    bool equipmentRequested =
        wantsWeapon
        || wantsSword
        || wantsAxe
        || wantsBow
        || wantsGun
        || wantsMace
        || wantsPolearm
        || wantsStaff
        || wantsFist
        || wantsDagger
        || wantsThrown
        || wantsCrossbow
        || wantsWand
        || wantsFishingPole
        || wantsShield;

    if (!equipmentRequested)
        return false;

    for (
        uint8 slot = EQUIPMENT_SLOT_START;
        slot < EQUIPMENT_SLOT_END;
        ++slot
    )
    {
        Item* item = candidate.bot->GetItemByPos(
            INVENTORY_SLOT_BAG_0, slot);

        if (!item)
            continue;

        ItemTemplate const* proto =
            item->GetTemplate();

        if (!proto)
            continue;

        uint32 itemClass = proto->Class;
        uint32 subClass = proto->SubClass;

        // Wrath ItemClass 2 is weapon.
        if (wantsWeapon && itemClass == 2)
            return true;

        if (itemClass == 2)
        {
            // Weapon subclass values are stable client data.
            if (
                wantsAxe
                && (subClass == 0 || subClass == 1)
            )
                return true;

            if (wantsBow && subClass == 2)
                return true;

            if (wantsGun && subClass == 3)
                return true;

            if (
                wantsMace
                && (subClass == 4 || subClass == 5)
            )
                return true;

            if (wantsPolearm && subClass == 6)
                return true;

            if (
                wantsSword
                && (subClass == 7 || subClass == 8)
            )
                return true;

            if (wantsStaff && subClass == 10)
                return true;

            if (wantsFist && subClass == 13)
                return true;

            if (wantsDagger && subClass == 15)
                return true;

            if (wantsThrown && subClass == 16)
                return true;

            if (wantsCrossbow && subClass == 18)
                return true;

            if (wantsWand && subClass == 19)
                return true;

            if (wantsFishingPole && subClass == 20)
                return true;
        }

        // Wrath ItemClass 4 / subclass 6 is shield.
        if (
            wantsShield
            && itemClass == 4
            && subClass == 6
        )
            return true;
    }

    return false;
}

uint32 ScoreContextCandidate(
    ProximityCandidate const& candidate,
    std::string const& messageLower)
{
    uint32 score = 0;

    std::string classLower =
        ToLowerAscii(candidate.className);

    if (
        !classLower.empty()
        && HasContextWord(messageLower, classLower)
    )
        score += 30;

    std::string raceLower =
        ToLowerAscii(candidate.raceName);

    if (
        !raceLower.empty()
        && HasContextWord(messageLower, raceLower)
    )
        score += 20;

    if (
        CandidateMatchesEquipmentContext(
            candidate, messageLower)
    )
        score += 50;

    return score;
}

bool LooksLikePlayerSayRetarget(
    std::string const& message)
{
    std::string lower = ToLowerAscii(message);

    if (lower.empty())
        return false;

    // Conservative by design. A factual topic by itself
    // must not steal an existing conversation.
    //
    // "nice mace"       -> can retarget
    // "your staff..."   -> can retarget
    // "hey priest"      -> can retarget
    // "are maces good?" -> normal continuity
    auto startsWithAny =
        [&](std::initializer_list<char const*> values)
    {
        for (char const* value : values)
        {
            std::string prefix(value);

            if (lower.rfind(prefix, 0) == 0)
                return true;
        }

        return false;
    };

    if (
        startsWithAny({
            "hey ",
            "yo ",
            "nice ",
            "cool ",
            "sick ",
            "sweet ",
            "love that ",
            "like that ",
            "look at that "
        })
    )
        return true;

    std::string padded = " " + lower + " ";

    for (
        char const* value :
        {
            " your ",
            " you're ",
            " youre ",
            " you are ",
            " that shield",
            " that staff",
            " that mace",
            " that sword",
            " that axe",
            " that bow",
            " that gun",
            " that wand",
            " that dagger",
            " that crossbow",
            " that mount",
            " that pet"
        }
    )
    {
        if (
            padded.find(value)
            != std::string::npos
        )
            return true;
    }

    return false;
}


std::vector<ProximityCandidate>
FindContextualCandidates(
    Player* player,
    std::vector<ProximityCandidate> const& candidates,
    std::string const& message)
{
    std::string messageLower = ToLowerAscii(message);

    uint32 bestScore = 0;
    float bestDistance = 0.0f;
    bool hasBest = false;

    std::vector<ProximityCandidate> matches;

    for (auto const& candidate : candidates)
    {
        uint32 score = ScoreContextCandidate(
            candidate, messageLower);

        if (!score)
            continue;

        float distance = 0.0f;

        if (player && candidate.bot)
        {
            distance =
                player->GetDistance(candidate.bot);
        }

        if (
            !hasBest
            || score > bestScore
        )
        {
            bestScore = score;
            bestDistance = distance;
            hasBest = true;

            matches.clear();
            matches.push_back(candidate);
            continue;
        }

        if (score != bestScore)
            continue;

        // Equal semantic relevance:
        // the physically closest bot is the natural
        // recipient of a local observation.
        if (distance < bestDistance)
        {
            bestDistance = distance;

            matches.clear();
            matches.push_back(candidate);
        }
    }

    return matches;
}

bool HandleExplicitCombatBotSocialAddress(
    Player* player, std::string const& safeMsg)
{
    if (!sLLMChatterConfig
        || !sLLMChatterConfig->_proxChatterStopAndChatEnable)
    {
        return false;
    }

    if (!player || !player->IsInWorld()
        || player->IsInCombat()
        || player->IsMounted()
        || player->IsFlying()
        || safeMsg.empty())
    {
        return false;
    }

    Map* map = player->GetMap();
    if (!map || map->IsRaid() || map->IsDungeon()
        || map->IsBattleground())
    {
        return false;
    }

    float radius = static_cast<float>(
        sLLMChatterConfig
            ->_proxChatterPlayerSayScanRadius);

    std::vector<ProximityCandidate> candidates;
    CollectNearbyCombatBotsForSocialAddress(
        player, radius, candidates);
    DeduplicateCandidates(candidates);

    if (candidates.empty())
        return false;

    ProximityCandidate const* addressed =
        FindNamedCandidate(
            player, candidates, safeMsg);

    if (!addressed)
    {
        addressed =
            FindUniquePartialNamedCandidate(
                candidates, safeMsg);
    }

    if (!addressed)
    {
        addressed =
            FindSelectedCandidate(
                player, candidates);
    }

    if (!addressed || !addressed->bot)
        return false;

    uint32 replyWindow =
        sLLMChatterConfig
            ->_proxChatterReplyWindowSeconds;

    time_t until = time(nullptr)
        + static_cast<time_t>(replyWindow);

    SetProximitySocialPauseUntil(
        addressed->bot, until);

    LOG_INFO(
        "module",
        "[LLMChatter][SOCIAL-PAUSE] "
        "player={} bot={} reason=addressed_in_combat until={}",
        player->GetName(),
        addressed->bot->GetName(),
        static_cast<long long>(until));

    // A directly addressed Playerbot may still answer while
    // fighting. Combat remains authoritative: the normal reply
    // delivery path will not create a physical stop-and-chat hold
    // because IsValidProximityChatHold() rejects combat bots.
    // The social timestamp survives solely to suppress the NEXT
    // voluntary AttackAnything pull after combat/looting.
    std::vector<ProximityCandidate> speaker = {
        *addressed};

    QueuePlayerSayProximityEvent(
        player,
        "proximity_player_say",
        speaker,
        candidates,
        1,
        safeMsg,
        addressed->name);

    // Consume normal routing so another idle bot cannot steal
    // a message explicitly addressed to this fighting bot.
    return true;
}
bool QueueNamedPlayerSayProximityEvent(
    Player* player, std::string const& safeMsg)
{
    if (!player || safeMsg.empty())
        return false;
    if (player->IsInCombat() || player->IsMounted()
        || player->IsFlying())
        return false;

    Map* map = player->GetMap();
    if (!map || map->IsRaid() || map->IsDungeon()
        || map->IsBattleground())
        return false;

    float radius = static_cast<float>(
        sLLMChatterConfig
            ->_proxChatterPlayerSayScanRadius);

    // Direct player /say interaction is for nearby
    // PlayerBots. Party membership does not matter:
    // local /say should still be able to address them.
    std::vector<ProximityCandidate> candidates;
    CollectNearbyBots(player, radius, candidates);
    DeduplicateCandidates(candidates);

    if (candidates.empty())
        return false;

    ProximityCandidate const* named =
        FindNamedCandidate(
            player, candidates, safeMsg);

    ProximityCandidate const* addressed = named;

    // Generated Playerbot names can be cumbersome. A unique
    // prefix such as "Alua" for "Aluatter" is still explicit
    // player addressing and must outrank an older scene.
    if (!addressed)
    {
        addressed =
            FindUniquePartialNamedCandidate(
                candidates,
                safeMsg);
    }

    if (!addressed)
        return false;

    // A human explicitly addressed this bot. Give it a short
    // listening pause immediately instead of letting ordinary
    // point travel continue during LLM generation latency.
    //
    // This is deliberately temporary. Successful delivery
    // upgrades it to the normal conversation hold below in
    // RecordDeliveredProximityLine().
    if (
        sLLMChatterConfig->_proxChatterStopAndChatEnable
        && addressed->bot
        && IsValidProximityChatHold(player, addressed->bot)
    )
    {
        SetProximitySocialPauseUntil(
            addressed->bot,
            time(nullptr) + 5);

        PauseProximityChatPointMotionFor(
            addressed->bot, 5000);
    }

    std::vector<ProximityCandidate> speaker = {
        *addressed};

    QueuePlayerSayProximityEvent(
        player,
        "proximity_player_say",
        speaker,
        candidates,
        1,
        safeMsg,
        addressed->name);

    return true;
}

void HandleProximityPlayerSayNewScene(
    Player* player, std::string const& safeMsg)
{
    if (!player || !player->IsInWorld())
        return;
    if (player->IsInCombat() || player->IsMounted()
        || player->IsFlying())
        return;

    Map* map = player->GetMap();
    if (!map || map->IsRaid() || map->IsDungeon()
        || map->IsBattleground())
        return;

    float radius = static_cast<float>(
        sLLMChatterConfig
            ->_proxChatterPlayerSayScanRadius);

    // Player /say is heard by nearby PlayerBots.
    // NPCs do not participate in normal player chat.
    std::vector<ProximityCandidate> candidates;
    CollectNearbyBots(player, radius, candidates);
    DeduplicateCandidates(candidates);

    // Filter cooled-down bots before selecting speakers.
    candidates.erase(
        std::remove_if(
            candidates.begin(), candidates.end(),
            [](ProximityCandidate const& c)
            {
                return IsEventOnCooldown(
                    _entityCooldowns,
                    GetEntityCooldownKey(c),
                    sLLMChatterConfig
                        ->_proxChatterEntityCooldown);
            }),
        candidates.end());

    if (candidates.empty())
        return;

    // Preserve the complete nearby roster for action
    // resolution. Speaker routing may safely narrow a copy.
    std::vector<ProximityCandidate> allCandidates =
        candidates;

    ProximityCandidate targetCandidate;
    bool hasTargetCandidate = false;

    // Explicit full/first-token names have highest priority.
    if (
        ProximityCandidate const* named =
            FindNamedCandidate(
                player, candidates, safeMsg)
    )
    {
        targetCandidate = *named;
        hasTargetCandidate = true;
    }

    // A unique name prefix such as "Alua" may address
    // Aluatter without requiring the complete generated name.
    if (!hasTargetCandidate)
    {
        if (
            ProximityCandidate const* partial =
                FindUniquePartialNamedCandidate(
                    candidates, safeMsg)
        )
        {
            targetCandidate = *partial;
            hasTargetCandidate = true;
        }
    }

    // An explicitly selected bot remains intentional routing.
    if (!hasTargetCandidate)
    {
        if (
            ProximityCandidate const* selected =
                FindSelectedCandidate(
                    player, candidates)
        )
        {
            targetCandidate = *selected;
            hasTargetCandidate = true;
        }
    }

    float observationRadius =
        static_cast<float>(
            sLLMChatterConfig
                ->_proxChatterPlayerObservationRadius);

    observationRadius = std::min(
        observationRadius,
        radius);

    std::vector<ProximityCandidate>
        observationCandidates;

    for (
        ProximityCandidate const& candidate :
        candidates)
    {
        if (
            candidate.bot
            && player->IsWithinDistInMap(
                candidate.bot,
                observationRadius)
        )
        {
            observationCandidates.push_back(
                candidate);
        }
    }

    std::vector<ProximityCandidate>
        contextualCandidates =
            FindContextualCandidates(
                player,
                observationCandidates,
                safeMsg);

    // Explicit name/selection routing keeps the normal /say
    // scan radius. Inferred routing must stay inside the tighter
    // observation radius so a generic local remark cannot summon
    // a distant bot merely because it can technically hear /say.
    std::vector<ProximityCandidate> speakerCandidates =
        hasTargetCandidate
            ? candidates
            : observationCandidates;

    if (
        !hasTargetCandidate
        && !contextualCandidates.empty()
    )
    {
        speakerCandidates = contextualCandidates;

        // FindContextualCandidates already resolves equal semantic
        // matches by physical distance. A unique factual match owns
        // the response.
        if (contextualCandidates.size() == 1)
        {
            targetCandidate =
                contextualCandidates.front();
            hasTargetCandidate = true;
        }
    }

    // No explicit target, no factual match, and nobody close enough
    // to be naturally addressed: do not infer a distant responder.
    if (
        !hasTargetCandidate
        && speakerCandidates.empty()
    )
    {
        return;
    }

    std::string addressedName =
        hasTargetCandidate
            ? targetCandidate.name
            : std::string();

    if (hasTargetCandidate)
    {
        // Explicit/contextual routing already owns the first speaker.
        // Preserve the existing randomized pool for any additional
        // conversational participants.
        std::shuffle(
            speakerCandidates.begin(),
            speakerCandidates.end(),
            _rng);
    }
    else
    {
        // Generic new /say such as "hey", "hello", or "sup":
        // nearest eligible PlayerBot inside the observation radius
        // gets first ownership. Additional nearby participants may
        // still vary if a multi-bot conversation is generated.
        std::sort(
            speakerCandidates.begin(),
            speakerCandidates.end(),
            [player](
                ProximityCandidate const& left,
                ProximityCandidate const& right)
            {
                if (!left.bot)
                    return false;
                if (!right.bot)
                    return true;

                return player->GetDistance(left.bot)
                    < player->GetDistance(right.bot);
            });

        if (speakerCandidates.size() > 2)
        {
            std::shuffle(
                speakerCandidates.begin() + 1,
                speakerCandidates.end(),
                _rng);
        }
    }

    bool wantsConversation =
        speakerCandidates.size() >= 2
        && urand(1, 100)
            <= sLLMChatterConfig
                   ->_proxChatterConversationChance;

    if (wantsConversation)
    {
        size_t participantCount = std::min<size_t>(
            speakerCandidates.size(), 3);

        std::vector<ProximityCandidate> speakers(
            speakerCandidates.begin(),
            speakerCandidates.begin()
                + participantCount);

        if (hasTargetCandidate)
        {
            bool found = false;

            for (size_t i = 0; i < speakers.size(); ++i)
            {
                if (SameCandidate(
                        speakers[i],
                        targetCandidate))
                {
                    std::swap(
                        speakers[0],
                        speakers[i]);
                    found = true;
                    break;
                }
            }

            if (!found)
                speakers[0] = targetCandidate;
        }

        uint32 maxLines = std::clamp<uint32>(
            sLLMChatterConfig
                ->_proxChatterMaxConversationLines,
            2, 4);

        QueuePlayerSayProximityEvent(
            player,
            "proximity_player_conversation",
            speakers,
            allCandidates,
            maxLines,
            safeMsg,
            addressedName);

        return;
    }

    ProximityCandidate chosen =
        hasTargetCandidate
            ? targetCandidate
            : speakerCandidates.front();

    std::vector<ProximityCandidate> speaker = {
        chosen};

    QueuePlayerSayProximityEvent(
        player,
        "proximity_player_say",
        speaker,
        allCandidates,
        1,
        safeMsg,
        addressedName);
}

void MaybeQueueProximityScene(Player* player)
{
    if (!player || !player->IsInWorld())
        return;
    if (player->IsInCombat() || player->IsMounted()
        || player->IsFlying())
        return;

    Map* map = player->GetMap();
    if (!map || map->IsRaid() || map->IsDungeon()
        || map->IsBattleground())
        return;

    uint32 effectiveChance =
        ComputeEffectiveChance(player);

    if (effectiveChance == 0
        || urand(1, 100) > effectiveChance)
        return;

    float radius = static_cast<float>(
        sLLMChatterConfig
            ->_proxChatterScanRadius);

    // Ambient proximity chatter represents nearby
    // PlayerBots talking in /say.
    std::vector<ProximityCandidate> candidates;
    CollectNearbyBots(player, radius, candidates);
    DeduplicateCandidates(candidates);

    if (candidates.empty())
        return;

    std::shuffle(
        candidates.begin(), candidates.end(),
        _rng);

    bool playerAddressed =
        urand(1, 100)
        <= sLLMChatterConfig
               ->_proxChatterPlayerAddressChance;

    bool wantsConversation =
        candidates.size() >= 2
        && urand(1, 100)
            <= sLLMChatterConfig
                   ->_proxChatterConversationChance;

    if (wantsConversation)
    {
        size_t participantCount = std::min<size_t>(
            candidates.size(), 3);

        std::vector<ProximityCandidate> speakers(
            candidates.begin(),
            candidates.begin() + participantCount);

        uint32 maxLines = std::clamp<uint32>(
            sLLMChatterConfig
                ->_proxChatterMaxConversationLines,
            2, 4);

        QueueProximityEvent(
            player,
            "proximity_conversation",
            speakers,
            candidates,
            playerAddressed,
            maxLines);

        return;
    }

    // Avoid duplicating idle party chatter for a
    // single ambient /say line when possible.
    Group* playerGroup = player->GetGroup();

    std::vector<ProximityCandidate> nonParty;
    for (auto const& c : candidates)
    {
        if (!IsSameGroup(c.bot, playerGroup))
            nonParty.push_back(c);
    }

    if (nonParty.empty())
        return;

    std::vector<ProximityCandidate> speaker = {
        nonParty.front()};

    QueueProximityEvent(
        player,
        "proximity_say",
        speaker,
        candidates,
        playerAddressed,
        1);
}

ProximityScene* FindBestScene(Player* player)
{
    if (!player)
        return nullptr;

    auto it = _playerScenes.find(
        player->GetGUID().GetCounter());
    if (it == _playerScenes.end())
        return nullptr;

    ProximityScene* best = nullptr;
    for (uint32 sceneId : it->second)
    {
        auto sceneIt = _activeScenes.find(sceneId);
        if (sceneIt == _activeScenes.end())
            continue;

        ProximityScene& scene = sceneIt->second;
        if (scene.IsExpired() || scene.pendingReply)
            continue;
        if (!scene.replyEligible)
            continue;
        if (scene.replyCount
            >= sLLMChatterConfig
                   ->_proxChatterReplyMaxTurns)
            continue;
        if (scene.mapId != player->GetMapId())
            continue;

        ProximityParticipant responder;
        responder.id = scene.lastSpeakerId;
        responder.isNPC = scene.lastSpeakerIsNPC;
        responder.name = scene.lastSpeakerName;
        WorldObject* target =
            ResolveParticipantObject(
                player, responder);
        if (!target)
            continue;
        if (!player->IsWithinDistInMap(target, 40.0f))
            continue;

        if (!best
            || scene.lastActivity
                > best->lastActivity)
            best = &scene;
    }

    return best;
}

std::string TrimChatMessage(
    std::string const& msg)
{
    size_t start =
        msg.find_first_not_of(" \t\r\n");
    if (start == std::string::npos)
        return "";

    size_t end =
        msg.find_last_not_of(" \t\r\n");
    std::string trimmed =
        msg.substr(start, end - start + 1);
    // UTF-8 safe clamp: never split a multi-byte character
    return NormalizeChatTextForDb(
        trimmed, sLLMChatterConfig->_maxMessageLength);
}
} // namespace

void CheckProximityChatter()
{
    if (!sLLMChatterConfig
        || !sLLMChatterConfig->IsEnabled()
        || !sLLMChatterConfig->_proxChatterEnable
        || !sLLMChatterConfig->_useEventSystem)
        return;

    EvictExpiredScenes();
    EvictExpiredProximityCooldowns();

    WorldSessionMgr::SessionMap const& sessions =
        sWorldSessionMgr->GetAllSessions();
    for (auto const& pair : sessions)
    {
        WorldSession* session = pair.second;
        if (!session || session->PlayerLoading())
            continue;

        Player* player = session->GetPlayer();
        if (!player || !player->IsInWorld()
            || IsPlayerBot(player))
            continue;

        MaybeQueueProximityScene(player);
    }
}

void HandleProximityPlayerSay(
    Player* player, uint32 type, uint32 language,
    std::string const& msg)
{
    if (!sLLMChatterConfig
        || !sLLMChatterConfig->IsEnabled()
        || !sLLMChatterConfig->_proxChatterEnable
        || !sLLMChatterConfig->_useEventSystem)
        return;

    if (!player || IsPlayerBot(player)
        || type != CHAT_MSG_SAY)
        return;

    // Ignore hidden addon traffic (DBM, Questie, ElvUI, ...);
    // it is real chat tagged LANG_ADDON, not player speech.
    if (language == LANG_ADDON)
    {
        LogIgnoredAddonChat(
            player, type, msg, "proximity");
        return;
    }

    EvictExpiredScenes();

    std::string safeMsg = TrimChatMessage(msg);
    if (safeMsg.empty())
        return;

    if (HandleExplicitCombatBotSocialAddress(
            player, safeMsg))
    {
        return;
    }

    if (QueueNamedPlayerSayProximityEvent(
            player, safeMsg))
        return;

    ProximityScene* scene = FindBestScene(player);

    // Player-initiated /say should continue an existing
    // scene only when its last speaker is a PlayerBot.
    // NPCs must not take ownership of normal player chat.
    if (scene && scene->lastSpeakerIsNPC)
        scene = nullptr;

    // Explicit full/partial names were already handled above.
    //
    // For otherwise ambiguous speech, a strong factual
    // observation in the CURRENT message may identify a
    // different nearby PlayerBot.
    //
    // The old speaker keeps ownership if they also satisfy
    // that current context.
    //
    // Example:
    //
    // Mage with staff answered:
    //   "nice staff"
    //       -> Mage remains speaker.
    //
    // Same Mage answered:
    //   "nice mace"
    //       -> If another nearby PlayerBot is the mace match,
    //          the stale Mage scene does not own this turn.
    //
    // Normal follow-ups such as "why?", "yeah", "really?",
    // and "how come?" never enter this branch.
    if (
        scene
        && LooksLikePlayerSayRetarget(safeMsg)
    )
    {
        float normalRadius =
            static_cast<float>(
                sLLMChatterConfig
                    ->_proxChatterPlayerSayScanRadius);

        float observationRadius =
            static_cast<float>(
                sLLMChatterConfig
                    ->_proxChatterPlayerObservationRadius);

        observationRadius = std::min(
            observationRadius,
            normalRadius);

        std::vector<ProximityCandidate> candidates;

        CollectNearbyBots(
            player,
            observationRadius,
            candidates);

        DeduplicateCandidates(candidates);

        std::vector<ProximityCandidate> contextual =
            FindContextualCandidates(
                player,
                candidates,
                safeMsg);

        bool currentSpeakerMatches = false;

        for (
            ProximityCandidate const& candidate :
            contextual)
        {
            if (
                !candidate.isNPC
                && candidate.id
                    == scene->lastSpeakerId
            )
            {
                currentSpeakerMatches = true;
                break;
            }
        }

        if (
            !contextual.empty()
            && !currentSpeakerMatches
        )
        {
            LOG_INFO(
                "module",
                "[LLMChatter][PROX-ROUTE] "
                "player={} old={} "
                "reason=current_context "
                "observation_radius={} message={}",
                player->GetName(),
                scene->lastSpeakerName,
                observationRadius,
                safeMsg);

            // We already have authoritative current-message
            // contextual matches and already proved that the
            // stale scene speaker is not one of them.
            //
            // Do NOT route through generic new-scene selection
            // again: an unrelated selected/sticky bot could
            // otherwise steal the factual observation.
            ProximityCandidate chosen =
                contextual.front();

            std::vector<ProximityCandidate> speaker = {
                chosen};

            QueuePlayerSayProximityEvent(
                player,
                "proximity_player_say",
                speaker,
                candidates,
                1,
                safeMsg,
                chosen.name);

            return;
        }
    }

    if (!scene)
    {
        HandleProximityPlayerSayNewScene(
            player, safeMsg);
        return;
    }

    ProximityParticipant responder;
    responder.id = scene->lastSpeakerId;
    responder.isNPC = false;
    responder.name = scene->lastSpeakerName;

    WorldObject* target =
        ResolveParticipantObject(player, responder);

    // If the old scene's bot is no longer available,
    // try another nearby PlayerBot instead of dropping
    // the player's message.
    if (!target)
    {
        HandleProximityPlayerSayNewScene(
            player, safeMsg);
        return;
    }

    std::string json = std::string("{")
        + "\"scene_id\":"
        + std::to_string(scene->sceneId)
        + ",\"player_guid\":"
        + std::to_string(
            player->GetGUID().GetCounter())
        + ",\"player_name\":\""
        + JsonEscape(player->GetName())
        + "\",\"player_message\":\""
        + JsonEscape(safeMsg)
        + "\",\"zone_name\":\""
        + JsonEscape(
            GetAreaNameForLocale(
                player->GetZoneId()))
        + "\",\"turn_count\":"
        + std::to_string(scene->replyCount)
        + ",\"last_message\":\""
        + JsonEscape(scene->lastMessage)
        + "\",\"responder_name\":\""
        + JsonEscape(scene->lastSpeakerName)
        + "\",\"responder_is_npc\":"
        + std::string(
            scene->lastSpeakerIsNPC
                ? "true"
                : "false")
        + ",\"responder_bot_guid\":"
        + std::to_string(
            scene->lastSpeakerIsNPC
                ? 0
                : scene->lastSpeakerId)
        + ",\"responder_npc_spawn_id\":"
        + std::to_string(
            scene->lastSpeakerIsNPC
                ? scene->lastSpeakerId
                : 0)
        + "}";

    QueueChatterEvent(
        "proximity_reply",
        "player",
        player->GetZoneId(),
        player->GetMapId(),
        GetChatterEventPriority(
            "proximity_reply"),
        "",
        scene->lastSpeakerIsNPC
            ? 0
            : scene->lastSpeakerId,
        scene->lastSpeakerName,
        player->GetGUID().GetCounter(),
        player->GetName(),
        0,
        EscapeString(json),
        GetReactionDelaySeconds(
            "proximity_reply"),
        sLLMChatterConfig
            ->_eventExpirationSeconds,
        false);

    scene->pendingReply = true;
    scene->lastActivity = time(nullptr);
}

void RecordDeliveredProximityLine(
    uint32 eventId, uint32 playerGuid,
    uint32 zoneId, uint32 botGuid,
    uint32 npcSpawnId, bool replyEligible,
    std::string const& speakerName,
    std::string const& message)
{
    if (!eventId || !playerGuid)
        return;

    EvictExpiredScenes();

    ProximityScene& scene = _activeScenes[eventId];
    bool wasPendingReply = scene.pendingReply;
    if (scene.sceneId == 0)
    {
        scene.sceneId = eventId;
        scene.playerGuid = playerGuid;
        scene.zoneId = zoneId;
        auto& ids = _playerScenes[playerGuid];
        if (std::find(
                ids.begin(), ids.end(), eventId)
            == ids.end())
            ids.push_back(eventId);
    }

    ObjectGuid playerObjGuid =
        ObjectGuid::Create<HighGuid::Player>(
            playerGuid);
    Player* player =
        ObjectAccessor::FindPlayer(playerObjGuid);
    if (player && player->IsInWorld())
        scene.mapId = player->GetMapId();

    bool isNPC = npcSpawnId != 0;
    uint32 speakerId = isNPC ? npcSpawnId : botGuid;
    if (speakerId)
    {
        AddSceneParticipant(
            scene, speakerId, isNPC, speakerName);
        scene.lastSpeakerId = speakerId;
        scene.lastSpeakerIsNPC = isNPC;
        scene.lastSpeakerName = speakerName;
    }

    scene.lastMessage = message;
    scene.lastActivity = time(nullptr);
    scene.replyEligible = replyEligible;
    scene.pendingReply = false;
    if (wasPendingReply && scene.replyCount < 255)
        ++scene.replyCount;

    // A delivered Playerbot /say line owns a temporary movement
    // hold while this scene remains open for another reply.
    // NPCs and party/raid Playerbots are never held.
    if (botGuid)
    {
        ObjectGuid botObjGuid =
            ObjectGuid::Create<HighGuid::Player>(
                botGuid);
        Player* bot =
            ObjectAccessor::FindPlayer(botObjGuid);

        if (replyEligible
            && IsValidProximityChatHold(player, bot))
        {
            ProximityChatHold& hold =
                _proximityChatHolds[botGuid];
            hold.playerGuid = playerGuid;
            hold.lastActivity = scene.lastActivity;

            uint32 replyWindow =
                sLLMChatterConfig
                    ->_proxChatterReplyWindowSeconds;

            SetProximitySocialPauseUntil(
                bot,
                scene.lastActivity
                    + static_cast<time_t>(replyWindow));

            PauseProximityChatPointMotion(bot);
        }
        else
        {
            auto holdIt = _proximityChatHolds.find(botGuid);
            if (holdIt != _proximityChatHolds.end())
            {
                ResumeProximityChatPointMotion(bot);

                if (!bot || !bot->IsInCombat())
                    ClearProximitySocialPause(bot);

                _proximityChatHolds.erase(holdIt);
            }
        }
    }
}

void UpdateProximityChatHolds()
{
    if (!sLLMChatterConfig
        || !sLLMChatterConfig
                ->_proxChatterStopAndChatEnable)
    {
        for (auto const& entry : _proximityChatHolds)
        {
            ObjectGuid botObjGuid =
                ObjectGuid::Create<HighGuid::Player>(
                    entry.first);
            Player* bot =
                ObjectAccessor::FindPlayer(botObjGuid);
            ResumeProximityChatPointMotion(bot);
            ClearProximitySocialPause(bot);
        }

        _proximityChatHolds.clear();
        return;
    }

    EvictExpiredScenes();

    uint32 expiry =
        sLLMChatterConfig
            ->_proxChatterReplyWindowSeconds;
    time_t now = time(nullptr);

    for (auto it = _proximityChatHolds.begin();
         it != _proximityChatHolds.end();)
    {
        uint32 botGuid = it->first;
        ProximityChatHold const& hold = it->second;

        bool expired =
            now - hold.lastActivity
                > static_cast<time_t>(expiry);

        ObjectGuid playerObjGuid =
            ObjectGuid::Create<HighGuid::Player>(
                hold.playerGuid);
        ObjectGuid botObjGuid =
            ObjectGuid::Create<HighGuid::Player>(
                botGuid);

        Player* player =
            ObjectAccessor::FindPlayer(playerObjGuid);
        Player* bot =
            ObjectAccessor::FindPlayer(botObjGuid);

        bool validHold =
            IsValidProximityChatHold(player, bot);

        if (expired || !validHold)
        {
            ResumeProximityChatPointMotion(bot);

            // Bot combat temporarily owns execution but does not
            // cancel conversational intent. Every other invalidation
            // ends the social pull pause immediately.
            bool preserveForBotCombat =
                !expired
                && bot
                && bot->IsInCombat()
                && player
                && player->IsInWorld()
                && player->IsAlive()
                && !player->IsInCombat();

            if (!preserveForBotCombat)
                ClearProximitySocialPause(bot);

            it = _proximityChatHolds.erase(it);
            continue;
        }

        // Keep ordinary active point travel stalled while the
        // conversation owns this hold. PauseMovement preserves
        // the PointMovementGenerator and destination, preventing
        // CityLife from inching between repeated StopMoving calls.
        // Rechecking each update also catches a replacement point
        // generator installed while the hold is still active.
        PauseProximityChatPointMotion(bot);
        ++it;
    }
}
