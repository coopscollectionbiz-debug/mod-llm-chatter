/*
 * mod-llm-chatter - player/general ownership
 */

#include "LLMChatterConfig.h"
#include "LLMChatterAmbient.h"
#include "LLMChatterBG.h"
#include "LLMChatterGroup.h"
#include "LLMChatterGroupInternal.h"
#include "LLMChatterShared.h"

#include "Battleground.h"
#include "BattlegroundAB.h"
#include "BattlegroundEY.h"
#include "BattlegroundWS.h"
#include "Channel.h"
#include "ChannelMgr.h"
#include "Chat.h"
#include "DatabaseEnv.h"
#include "DBCStores.h"
#include "Group.h"
#include "Log.h"
#include "MapMgr.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "ItemTemplate.h"
#include "SkillDiscovery.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "ScriptMgr.h"
#include "World.h"
#include "WorldSession.h"
#include "WorldSessionMgr.h"

#include <algorithm>
#include <cctype>
#include <ctime>
#include <cstdio>
#include <limits>
#include <map>
#include <mutex>
#include <random>
#include <string>
#include <vector>

Channel* EnsureBotInChatChannel(
    Player* bot, uint32 channelId)
{
    if (!bot || !bot->IsInWorld())
        return nullptr;

    ChatChannelsEntry const* chEntry =
        sChatChannelsStore.LookupEntry(channelId);
    if (!chEntry)
        return nullptr;

    uint8 locale = sWorld->GetDefaultDbcLocale();

    char const* pattern = chEntry->pattern[locale];
    if (!pattern || !*pattern)
        pattern = chEntry->pattern[LOCALE_enUS];
    if (!pattern || !*pattern)
        return nullptr;

    std::string newChanName;

    if (channelId == ChatChannelId::GENERAL
        || channelId == ChatChannelId::LOCAL_DEFENSE)
    {
        AreaTableEntry const* area =
            sAreaTableStore.LookupEntry(
                bot->GetZoneId());
        if (!area)
            return nullptr;

        char const* n = area->area_name[locale];
        std::string zoneName = n ? n : "";
        if (zoneName.empty())
        {
            n = area->area_name[LOCALE_enUS];
            zoneName = n ? n : "";
        }
        if (zoneName.empty())
            return nullptr;

        char nameBuf[100];
        std::snprintf(
            nameBuf,
            sizeof(nameBuf),
            pattern,
            zoneName.c_str());

        newChanName = nameBuf;
    }
    else if (channelId == ChatChannelId::TRADE
        || channelId
            == ChatChannelId::GUILD_RECRUITMENT)
    {
        AreaTableEntry const* cityArea =
            sAreaTableStore.LookupEntry(3459);
        if (!cityArea)
            return nullptr;

        char const* n =
            cityArea->area_name[locale];
        std::string cityName = n ? n : "";
        if (cityName.empty())
        {
            n = cityArea->area_name[LOCALE_enUS];
            cityName = n ? n : "";
        }
        if (cityName.empty())
            return nullptr;

        char nameBuf[100];
        std::snprintf(
            nameBuf,
            sizeof(nameBuf),
            pattern,
            cityName.c_str());

        newChanName = nameBuf;
    }
    else if (
        channelId
            == ChatChannelId::LOOKING_FOR_GROUP
        || channelId
            == ChatChannelId::WORLD_DEFENSE)
    {
        newChanName = pattern;
    }
    else
    {
        return nullptr;
    }

    if (newChanName.empty())
        return nullptr;

    ChannelMgr* cMgr =
        ChannelMgr::forTeam(bot->GetTeamId());
    if (!cMgr)
        return nullptr;

    static std::mutex channelsLock;
    std::lock_guard<std::mutex> guard(
        channelsLock);

    for (auto const& [key, channel] :
         cMgr->GetChannels())
    {
        if (!channel)
            continue;
        if (channel->GetChannelId()
            != channelId)
            continue;
        if (channel->GetName()
            == newChanName)
            continue;

        channel->LeaveChannel(bot, false);
        bot->LeftChannel(channel);
    }

    Channel* joinChan =
        cMgr->GetJoinChannel(
            newChanName,
            channelId);

    if (joinChan)
        joinChan->JoinChannel(bot, "");

    return joinChan;
}

void EnsureBotInGeneralChannel(
    Player* bot)
{
    EnsureBotInChatChannel(
        bot,
        ChatChannelId::GENERAL);
}

static bool CanLLMServiceBotCastSpell(
    Player* bot,
    uint32 spellId)
{
    if (!bot || !spellId)
        return false;

    if (
        !bot->IsInWorld()
        || !bot->IsAlive())
    {
        return false;
    }

    return bot->HasSpell(spellId);
}

static bool GetLLMLockboxRequiredSkill(
    uint32 itemEntry,
    uint32& requiredSkill)
{
    requiredSkill = 0;

    if (!itemEntry)
        return false;

    ItemTemplate const* itemTemplate =
        sObjectMgr->GetItemTemplate(
            itemEntry);

    if (
        !itemTemplate
        || !itemTemplate->LockID)
    {
        return false;
    }

    LockEntry const* lockInfo =
        sLockStore.LookupEntry(
            itemTemplate->LockID);

    if (!lockInfo)
        return false;

    for (uint8 i = 0; i < 8; ++i)
    {
        if (
            lockInfo->Type[i]
            != LOCK_KEY_SKILL)
        {
            continue;
        }

        uint32 skillId =
            SkillByLockType(
                LockType(
                    lockInfo->Index[i]));

        if (
            skillId
            != SKILL_LOCKPICKING)
        {
            continue;
        }

        requiredSkill =
            lockInfo->Skill[i];

        return true;
    }

    return false;
}

static bool CanLLMServiceBotOpenLockbox(
    Player* bot,
    uint32 itemEntry,
    uint32* requiredSkillOut = nullptr)
{
    if (
        !bot
        || bot->getClass()
            != CLASS_ROGUE
        || !bot->IsInWorld()
        || !bot->IsAlive())
    {
        return false;
    }

    constexpr uint32 PICK_LOCK_SPELL_ID =
        1804;

    if (
        !bot->HasSpell(
            PICK_LOCK_SPELL_ID)
        || !bot->HasSkill(
            SKILL_LOCKPICKING))
    {
        return false;
    }

    uint32 requiredSkill = 0;

    if (
        !GetLLMLockboxRequiredSkill(
            itemEntry,
            requiredSkill))
    {
        return false;
    }

    if (requiredSkillOut)
    {
        *requiredSkillOut =
            requiredSkill;
    }

    uint32 botSkill =
        bot->GetSkillValue(
            SKILL_LOCKPICKING);

    return botSkill >=
        requiredSkill;
}

static std::map<uint32, time_t> _generalChatCooldowns;
static std::mutex _generalChatCooldownsMutex;

// Mage-service discovery in General is intentionally separate
// from ambient General chatter. A legitimate service request
// must not consume or depend on the ambient zone cooldown.
static std::map<uint32, time_t> _generalServiceCooldowns;
static std::mutex _generalServiceCooldownsMutex;

static constexpr time_t GENERAL_SERVICE_COOLDOWN_SECONDS = 10;

static uint32 ExtractFirstLinkedItemEntry(
    std::string const& message)
{
    static std::string const marker =
        "|Hitem:";

    size_t pos =
        message.find(marker);

    if (pos == std::string::npos)
        return 0;

    pos += marker.size();

    size_t end = pos;

    while (
        end < message.size()
        && std::isdigit(
            static_cast<unsigned char>(
                message[end])))
    {
        ++end;
    }

    if (end == pos)
        return 0;

    try
    {
        unsigned long value =
            std::stoul(
                message.substr(
                    pos,
                    end - pos));

        if (
            value == 0
            || value
                > std::numeric_limits<uint32>::max())
        {
            return 0;
        }

        return static_cast<uint32>(
            value);
    }
    catch (...)
    {
        return 0;
    }
}

static bool LooksLikeGeneralLockpickServiceRequest(
    std::string const& message,
    uint32& itemEntry)
{
    itemEntry = 0;

    if (message.empty())
        return false;

    std::string lower = message;

    std::transform(
        lower.begin(),
        lower.end(),
        lower.begin(),
        [](unsigned char c)
        {
            return static_cast<char>(
                std::tolower(c));
        });

    bool hasDirectLockAction =
        lower.find("open")
            != std::string::npos
        || lower.find("pick")
            != std::string::npos
        || lower.find("unlock")
            != std::string::npos;

    bool hasHelpCue =
        lower.find("help")
            != std::string::npos
        || lower.find("assist")
            != std::string::npos;

    bool mentionsLockService =
        lower.find("lockbox")
            != std::string::npos
        || lower.find("lock box")
            != std::string::npos
        || lower.find("lockpick")
            != std::string::npos
        || lower.find("pick lock")
            != std::string::npos
        || lower.find("unlock")
            != std::string::npos
        || lower.find("rogue")
            != std::string::npos
        || lower.find("open")
            != std::string::npos;

    bool hasRequestCue =
        lower.find('?')
            != std::string::npos
        || lower.find("need")
            != std::string::npos
        || lower.find("lf")
            != std::string::npos
        || lower.find("looking")
            != std::string::npos
        || lower.find("can")
            != std::string::npos
        || lower.find("could")
            != std::string::npos
        || lower.find("please")
            != std::string::npos
        || lower.find("pls")
            != std::string::npos
        || lower.find("plz")
            != std::string::npos;

    itemEntry =
        ExtractFirstLinkedItemEntry(
            message);

    if (!itemEntry)
        return false;

    uint32 requiredSkill = 0;

    if (
        !GetLLMLockboxRequiredSkill(
            itemEntry,
            requiredSkill))
    {
        return false;
    }

    // The linked item's actual lock data establishes that
    // this is a lockpicking service. Player wording only
    // determines whether they are asking for that service.
    bool asksForLockService =
        hasDirectLockAction
        || hasHelpCue
        || (
            mentionsLockService
            && hasRequestCue
        );

    return asksForLockService;
}

static bool LooksLikeGeneralMageServiceRequest(
    std::string const& message)
{
    if (message.empty())
        return false;

    std::string lower = message;

    std::transform(
        lower.begin(),
        lower.end(),
        lower.begin(),
        [](unsigned char c)
        {
            return static_cast<char>(
                std::tolower(c));
        });

    auto hasWord =
        [&lower](std::string const& word)
        {
            size_t pos = 0;

            while (
                (pos = lower.find(word, pos))
                != std::string::npos)
            {
                bool leftOk =
                    pos == 0
                    || !std::isalnum(
                        static_cast<unsigned char>(
                            lower[pos - 1]));

                size_t end =
                    pos + word.size();

                bool rightOk =
                    end >= lower.size()
                    || !std::isalnum(
                        static_cast<unsigned char>(
                            lower[end]));

                if (leftOk && rightOk)
                    return true;

                pos = end;
            }

            return false;
        };

    // This is deliberately only a cheap pre-gate.
    // Python QuickAnalyze remains authoritative about whether
    // the message is really asking for a supported Mage service.
    //
    // Players use extremely terse service shorthand in General,
    // so do not require a separate "can/need/please" request cue.
    bool hasMageService =
        hasWord("water")
        || hasWord("food")
        || hasWord("bread")
        || hasWord("drink")
        || hasWord("conjure")
        || hasWord("port")
        || hasWord("portal")
        || hasWord("tele")
        || hasWord("teleport");

    bool hasPortalDestinationCue =
        hasWord("sw")
        || hasWord("stormwind")
        || hasWord("if")
        || hasWord("ironforge")
        || hasWord("darn")
        || hasWord("darnassus")
        || hasWord("exo")
        || hasWord("exodar")
        || hasWord("org")
        || hasWord("orgri")
        || hasWord("orgrimmar")
        || hasWord("uc")
        || hasWord("undercity")
        || hasWord("tb")
        || hasWord("silvermoon")
        || hasWord("smc")
        || hasWord("shatt")
        || hasWord("shat")
        || hasWord("shattrath")
        || hasWord("dal")
        || hasWord("dala")
        || hasWord("dalaran")
        || hasWord("thera")
        || hasWord("theramore")
        || hasWord("ston")
        || hasWord("stonard");

    return
        hasMageService
        || (
            hasWord("mage")
            && hasPortalDestinationCue
        );
}

// Per-group subzone cooldown keyed by group counter.
// Uses the same configured cooldown as
// GroupChatter.ZoneTransitionCooldown.
static std::map<uint32, time_t>
    _subzoneCommentCooldowns;
static std::mutex _subzoneCooldownMutex;

struct IntrusionState
{
    time_t firstSeen;
    bool firstAlerted;
};

static std::map<std::pair<uint32, uint32>,
    IntrusionState> _intrusionStates;
static std::map<uint32, time_t>
    _zoneAlertThrottle;
static time_t _lastIntrusionEviction = 0;
static std::mutex _intrusionMutex;

static std::map<uint32, std::pair<time_t, std::string>>
    _travelStateCache;
static std::mutex _travelStateMutex;

static void MaybePersistBotTravelState(
    Player* bot, uint32 groupId, bool force = false)
{
    if (!bot || !IsPlayerBot(bot) || !groupId)
        return;

    time_t now = time(nullptr);
    std::string signature =
        std::to_string(bot->GetZoneId()) + ":" +
        std::to_string(bot->GetAreaId()) + ":" +
        std::to_string(bot->GetMapId()) + ":" +
        std::to_string(bot->GetMountID()) + ":" +
        BuildBotTravelStateJson(bot);

    {
        std::lock_guard<std::mutex> guard(
            _travelStateMutex);
        auto it = _travelStateCache.find(
            bot->GetGUID().GetCounter());
        if (!force && it != _travelStateCache.end())
        {
            bool checkedRecently =
                (now - it->second.first) < 5;
            bool unchanged =
                it->second.second == signature;
            bool freshEnough =
                (now - it->second.first) < 60;
            if (checkedRecently
                || (unchanged && freshEnough))
                return;
        }

        _travelStateCache[
            bot->GetGUID().GetCounter()] =
            {now, signature};
    }

    UpdateGroupBotTravelState(bot, groupId);
}

static void RefreshGroupBotTravelStates(
    Player* player, bool force = false)
{
    if (!player || !sLLMChatterConfig
        || !sLLMChatterConfig->_useGroupChatter)
        return;

    Group* group = player->GetGroup();
    if (!group || !GroupHasBots(group))
        return;

    uint32 groupId = group->GetGUID().GetCounter();
    if (IsPlayerBot(player))
    {
        MaybePersistBotTravelState(player, groupId, force);
        return;
    }

    for (auto const& ref : group->GetMemberSlots())
    {
        Player* member =
            ObjectAccessor::FindPlayer(ref.guid);
        if (!member || !IsPlayerBot(member)
            || !member->IsInWorld())
            continue;
        MaybePersistBotTravelState(
            member, groupId, force);
    }
}

static void CleanupIntrusionStateOnZoneChange(
    uint32 guid, uint32 newZone)
{
    auto it = _intrusionStates.begin();
    while (it != _intrusionStates.end())
    {
        if (it->first.first == guid
            && it->first.second != newZone)
            it = _intrusionStates.erase(it);
        else
            ++it;
    }
}

static bool CheckIntrusionGate(
    uint32 guid, uint32 zoneId, time_t now)
{
    // Zone-level throttle
    auto zt = _zoneAlertThrottle.find(zoneId);
    if (zt != _zoneAlertThrottle.end()
        && (now - zt->second)
           < (time_t)sLLMChatterConfig
               ->_zoneIntrusionZoneThrottleSec)
        return false;

    // Per-intruder first-alert check
    auto key = std::make_pair(guid, zoneId);
    auto it = _intrusionStates.find(key);
    if (it != _intrusionStates.end()
        && it->second.firstAlerted)
        return false;

    return true;
}

static void CommitIntrusionState(
    uint32 guid, uint32 zoneId, time_t now)
{
    auto key = std::make_pair(guid, zoneId);
    _intrusionStates[key] = {now, true};
    _zoneAlertThrottle[zoneId] = now;
}

static void HandleEnemyZoneIntrusion(
    Player* player, uint32 newZone)
{
    if (!sLLMChatterConfig->_zoneIntrusionEnable)
        return;

    AreaTableEntry const* area =
        sAreaTableStore.LookupEntry(newZone);
    if (!area)
        return;

    // Only faction-owned zones
    if (area->team != AREATEAM_ALLY
        && area->team != AREATEAM_HORDE)
        return;

    // Determine if intruder is enemy
    TeamId playerTeam = player->GetTeamId();
    bool isEnemy = false;
    TeamId defenderTeam;
    if (area->team == AREATEAM_ALLY
        && playerTeam == TEAM_HORDE)
    {
        isEnemy = true;
        defenderTeam = TEAM_ALLIANCE;
    }
    else if (area->team == AREATEAM_HORDE
        && playerTeam == TEAM_ALLIANCE)
    {
        isEnemy = true;
        defenderTeam = TEAM_HORDE;
    }

    if (!isEnemy)
        return;

    uint32 guid = player->GetGUID().GetCounter();
    time_t now = time(nullptr);

    // First gate check (cheap, under mutex)
    {
        std::lock_guard<std::mutex> guard(
            _intrusionMutex);
        if (!CheckIntrusionGate(
                guid, newZone, now))
            return;
    }

    // Defender search (expensive, no lock held)
    Player* defender = FindNearbyDefenderBot(
        player, newZone, defenderTeam);
    if (!defender)
        return;

    // Re-check gate + commit atomically
    {
        std::lock_guard<std::mutex> guard(
            _intrusionMutex);
        if (!CheckIntrusionGate(
                guid, newZone, now))
            return;
        CommitIntrusionState(guid, newZone, now);
    }

    bool isCapital =
        (area->flags & AREA_FLAG_CAPITAL) != 0;

    std::string zoneName = GetZoneName(newZone);
    if (zoneName.empty())
        zoneName = "Unknown";

    uint8 intruderClass = player->getClass();
    uint8 intruderRace = player->getRace();
    uint32 intruderLevel = player->GetLevel();

    uint32 defGuid =
        defender->GetGUID().GetCounter();
    uint8 defClass = defender->getClass();
    uint8 defRace = defender->getRace();
    uint32 defLevel = defender->GetLevel();

    std::string extraData = "{"
        "\"intruder_name\":\"" +
            JsonEscape(player->GetName()) + "\","
        "\"intruder_class\":" +
            std::to_string(intruderClass) + ","
        "\"intruder_race\":" +
            std::to_string(intruderRace) + ","
        "\"intruder_level\":" +
            std::to_string(intruderLevel) + ","
        "\"intruder_is_bot\":false,"
        "\"is_capital\":" +
            std::string(
                isCapital ? "true" : "false") + ","
        "\"zone_name\":\"" +
            JsonEscape(zoneName) + "\","
        "\"defender_guid\":" +
            std::to_string(defGuid) + ","
        "\"defender_name\":\"" +
            JsonEscape(defender->GetName()) + "\","
        "\"defender_class\":" +
            std::to_string(defClass) + ","
        "\"defender_race\":" +
            std::to_string(defRace) + ","
        "\"defender_level\":" +
            std::to_string(defLevel) +
        "}";

    extraData = EscapeString(extraData);

    QueueChatterEvent(
        "player_enters_zone",
        "zone",
        newZone,
        player->GetMapId(),
        GetChatterEventPriority(
            "player_enters_zone"),
        "zone_intrusion:" +
            std::to_string(newZone),
        player->GetGUID().GetCounter(),
        player->GetName(),
        defGuid,
        defender->GetName(),
        0,
        extraData,
        GetReactionDelaySeconds(
            "player_enters_zone"),
        120,
        false
    );

}

static void HandleBotEntersEnemyTerritory(
    Player* player, uint32 newZone)
{
    if (!sLLMChatterConfig->_zoneIntrusionEnable)
        return;

    AreaTableEntry const* area =
        sAreaTableStore.LookupEntry(newZone);
    if (!area)
        return;

    if (area->team != AREATEAM_ALLY
        && area->team != AREATEAM_HORDE)
        return;

    TeamId playerTeam = player->GetTeamId();
    bool isEnemy = false;
    TeamId defenderTeam;
    if (area->team == AREATEAM_ALLY
        && playerTeam == TEAM_HORDE)
    {
        isEnemy = true;
        defenderTeam = TEAM_ALLIANCE;
    }
    else if (area->team == AREATEAM_HORDE
        && playerTeam == TEAM_ALLIANCE)
    {
        isEnemy = true;
        defenderTeam = TEAM_HORDE;
    }

    if (!isEnemy)
        return;

    // Only fire when a real human player is in the
    // target zone to witness the yell — no point
    // generating LLM chatter for bot-vs-bot events
    // in empty zones the user will never see.
    {
        bool hasRealPlayer = false;
        uint32 intruderMap = player->GetMapId();
        for (auto const& [pguid, p] :
             ObjectAccessor::GetPlayers())
        {
            if (!p || !p->IsInWorld())
                continue;
            if (IsPlayerBot(p))
                continue;
            if (p->GetZoneId() != newZone)
                continue;
            if (p->GetMapId() != intruderMap)
                continue;
            hasRealPlayer = true;
            break;
        }
        if (!hasRealPlayer)
            return;
    }

    uint32 guid = player->GetGUID().GetCounter();
    time_t now = time(nullptr);

    // First gate check (cheap, under mutex)
    {
        std::lock_guard<std::mutex> guard(
            _intrusionMutex);
        if (!CheckIntrusionGate(
                guid, newZone, now))
            return;
    }

    // Defender search (expensive, no lock held)
    Player* defender = FindNearbyDefenderBot(
        player, newZone, defenderTeam);
    if (!defender)
        return;

    // Re-check gate + commit atomically
    {
        std::lock_guard<std::mutex> guard(
            _intrusionMutex);
        if (!CheckIntrusionGate(
                guid, newZone, now))
            return;
        CommitIntrusionState(guid, newZone, now);
    }

    std::string zoneName = GetZoneName(newZone);
    if (zoneName.empty())
        zoneName = "Unknown";

    uint8 intruderClass = player->getClass();
    uint8 intruderRace = player->getRace();
    uint32 intruderLevel = player->GetLevel();

    uint32 defGuid =
        defender->GetGUID().GetCounter();

    std::string extraData = "{"
        "\"intruder_name\":\"" +
            JsonEscape(player->GetName()) + "\","
        "\"intruder_class\":" +
            std::to_string(intruderClass) + ","
        "\"intruder_race\":" +
            std::to_string(intruderRace) + ","
        "\"intruder_level\":" +
            std::to_string(intruderLevel) + ","
        "\"intruder_is_bot\":true,"
        "\"is_capital\":true,"
        "\"zone_name\":\"" +
            JsonEscape(zoneName) + "\","
        "\"defender_guid\":" +
            std::to_string(defGuid) + ","
        "\"defender_name\":\"" +
            JsonEscape(defender->GetName()) + "\","
        "\"defender_class\":" +
            std::to_string(
                defender->getClass()) + ","
        "\"defender_race\":" +
            std::to_string(
                defender->getRace()) + ","
        "\"defender_level\":" +
            std::to_string(
                defender->GetLevel()) +
        "}";

    extraData = EscapeString(extraData);

    QueueChatterEvent(
        "player_enters_zone",
        "zone",
        newZone,
        player->GetMapId(),
        GetChatterEventPriority(
            "player_enters_zone"),
        "zone_intrusion:" +
            std::to_string(newZone),
        player->GetGUID().GetCounter(),
        player->GetName(),
        defGuid,
        defender->GetName(),
        0,
        extraData,
        GetReactionDelaySeconds(
            "player_enters_zone"),
        120,
        false
    );

}

// ============================================================
// Delayed rejoin queue — definitions + processing
// ============================================================

std::mutex _rejoinMutex;
std::vector<PendingRejoin> _pendingRejoins;

void ProcessPendingRejoins()
{
    if (!sLLMChatterConfig->_useGroupChatter)
        return;

    std::vector<PendingRejoin> work;
    {
        std::lock_guard<std::mutex> guard(
            _rejoinMutex);
        if (_pendingRejoins.empty())
            return;
        work.swap(_pendingRejoins);
    }

    time_t now = time(nullptr);
    std::vector<PendingRejoin> deferred;

    for (auto const& entry : work)
    {
        // Minimum 10s before first attempt.
        if (now - entry.loginTime < 10)
        {
            deferred.push_back(entry);
            continue;
        }

        bool timedOut =
            (now - entry.loginTime > 120);

        Player* player =
            ObjectAccessor::FindPlayer(
                ObjectGuid::Create<HighGuid::Player>(
                    entry.playerGuid));
        if (!player)
            continue;

        if (IsPlayerBot(player))
            continue;

        Group* group = player->GetGroup();
        if (!group)
            continue;
        if (group->GetGUID().GetCounter()
            != entry.groupId)
            continue;

        // Check if all non-self members have loaded
        // Player* objects. Uses the persisted member
        // list (GetMemberSlots) for the total count
        // and live GroupReference for loaded count.
        // No bot-type filtering — avoids coupling
        // to mod-playerbots eligibility categories.
        auto const& slots =
            group->GetMemberSlots();
        uint32 totalOther = 0;
        for (auto const& slot : slots)
        {
            if (slot.guid.GetCounter()
                != entry.playerGuid)
                ++totalOther;
        }
        uint32 loadedOther = 0;
        for (GroupReference* itr =
                 group->GetFirstMember();
             itr != nullptr; itr = itr->next())
        {
            Player* m = itr->GetSource();
            if (m && m->GetGUID().GetCounter()
                != entry.playerGuid)
                ++loadedOther;
        }

        // Defer if members still loading, unless
        // the hard cap (120s) has been reached —
        // then process whatever is available.
        if (loadedOther < totalOther && !timedOut)
        {
            deferred.push_back(entry);
            continue;
        }

        // All members loaded — collect bot data.
        uint32 groupId = entry.groupId;
        uint32 pZone = player->GetZoneId();
        uint32 pArea = player->GetAreaId();
        uint32 pMap  = player->GetMapId();

        std::vector<uint32> botGuids;
        std::string firstBotName;
        std::string botsJson = "[";
        bool first = true;

        for (GroupReference* itr =
                 group->GetFirstMember();
             itr != nullptr; itr = itr->next())
        {
            Player* member = itr->GetSource();
            if (!member
                || !IsPlayerBot(member))
                continue;

            uint32 bguid =
                member->GetGUID().GetCounter();
            botGuids.push_back(bguid);
            if (first)
                firstBotName = member->GetName();

            std::string role = "dps";
            PlayerbotAI* ai =
                GET_PLAYERBOT_AI(member);
            if (ai)
            {
                if (PlayerbotAI::IsTank(member))
                    role = "tank";
                else if (
                    PlayerbotAI::IsHeal(member))
                    role = "healer";
                else if (
                    PlayerbotAI::IsRanged(member))
                    role = "ranged_dps";
                else
                    role = "melee_dps";
            }

            if (!first) botsJson += ",";
            first = false;
            botsJson += "{"
                "\"bot_guid\":" +
                    std::to_string(bguid) + ","
                "\"bot_name\":\"" +
                    JsonEscape(
                        member->GetName()) +
                    "\","
                "\"bot_class\":" +
                    std::to_string(
                        member->getClass()) +
                    ","
                "\"bot_race\":" +
                    std::to_string(
                        member->getRace()) +
                    ","
                "\"bot_gender\":" +
                    std::to_string(
                        member->getGender()) +
                    ","
                "\"bot_level\":" +
                    std::to_string(
                        member->GetLevel()) +
                    ","
                "\"role\":\"" + role + "\","
                "\"zone\":" +
                    std::to_string(pZone) + ","
                "\"map\":" +
                    std::to_string(pMap) +
                "}";
        }

        if (botGuids.empty())
            continue;

        botsJson += "]";

        std::string extraData = "{"
            "\"group_id\":" +
                std::to_string(groupId) + ","
            "\"player_name\":\"" +
                JsonEscape(player->GetName()) +
                "\","
            "\"player_guid\":" +
                std::to_string(
                    player->GetGUID().GetCounter()) + ","
            "\"zone\":" +
                std::to_string(pZone) + ","
            "\"area\":" +
                std::to_string(pArea) + ","
            "\"map\":" +
                std::to_string(pMap) + ","
            "\"rejoin\":true,"
            "\"bots\":" + botsJson +
            "}";
        extraData = EscapeString(extraData);

        QueueChatterEvent(
            "bot_group_join_batch",
            "player",
            pZone, pMap,
            GetChatterEventPriority(
                "bot_group_join_batch"), "",
            botGuids[0], firstBotName,
            0, "", 0,
            extraData,
            GetReactionDelaySeconds(
                "bot_group_join_batch"),
            120, false);

        // Mark bots as greeted.
        {
            std::lock_guard<std::mutex> guard(
                _groupJoinBatchMutex);
            for (uint32 bguid : botGuids)
            {
                _greetedBotGuids.insert(bguid);
                _groupGreetedBots[groupId]
                    .push_back(bguid);
            }
            _groupJoinFlushed.insert(groupId);
        }
    }

    // Re-insert entries that haven't aged enough.
    if (!deferred.empty())
    {
        std::lock_guard<std::mutex> guard(
            _rejoinMutex);
        _pendingRejoins.insert(
            _pendingRejoins.end(),
            deferred.begin(),
            deferred.end());
    }
}

class LLMChatterPlayerScript : public PlayerScript
{
public:
    LLMChatterPlayerScript()
        : PlayerScript(
              "LLMChatterPlayerScript",
              {PLAYERHOOK_ON_LOGIN,
               PLAYERHOOK_ON_UPDATE,
               PLAYERHOOK_CAN_PLAYER_USE_CHANNEL_CHAT,
               PLAYERHOOK_CAN_PLAYER_USE_PRIVATE_CHAT,
               PLAYERHOOK_ON_UPDATE_ZONE,
               PLAYERHOOK_ON_UPDATE_AREA,
               PLAYERHOOK_ON_PVP_KILL}) {}

    void OnPlayerLogin(Player* player) override
    {
        if (!sLLMChatterConfig
            || !sLLMChatterConfig->IsEnabled())
            return;

        if (!player || IsPlayerBot(player))
            return;

        // Defensive crash-recovery cleanup: if the
        // server crashed, CleanupGroupSession never
        // ran and stale entries may linger. Grace
        // 5-minute window: entries this old are
        // definitively stale (crash recovery only).
        // Protects active sessions of other online
        // players — normal delivery completes in
        // seconds, never minutes.
        CharacterDatabase.Execute(
            "UPDATE llm_chatter_queue "
            "SET status = 'cancelled' "
            "WHERE status = 'pending' "
            "AND created_at < NOW() "
            "- INTERVAL 5 MINUTE");

        CharacterDatabase.Execute(
            "UPDATE llm_chatter_messages "
            "SET delivered = 1 "
            "WHERE delivered = 0 "
            "AND deliver_at < NOW() "
            "- INTERVAL 5 MINUTE");

        // Re-initialize group state after relog.
        // Python cleanup_stale_groups() deletes
        // bot_traits while the player is offline;
        // OnAddMember doesn't fire on relog because
        // the group already exists.  Bots aren't
        // loaded yet at login time (PlayerbotMgr
        // spawns them asynchronously), so queue for
        // delayed processing from OnUpdate.
        if (!sLLMChatterConfig->_useGroupChatter)
            return;

        Group* group = player->GetGroup();
        if (!group)
            return;

        uint32 groupId =
            group->GetGUID().GetCounter();

        // Clear dedup state so the rejoin batch
        // can be queued when bots are loaded.
        {
            std::lock_guard<std::mutex> guard(
                _groupJoinBatchMutex);
            _groupJoinFlushed.erase(groupId);
            auto git =
                _groupGreetedBots.find(groupId);
            if (git != _groupGreetedBots.end())
            {
                for (uint32 bguid : git->second)
                    _greetedBotGuids.erase(bguid);
                _groupGreetedBots.erase(git);
            }
        }

        // Queue for delayed processing.
        {
            std::lock_guard<std::mutex> guard(
                _rejoinMutex);
            _pendingRejoins.push_back(
                {groupId,
                 player->GetGUID().GetCounter(),
                 time(nullptr)});
        }
    }

    // -------------------------------------------------
    // Private player -> PlayerBot whisper conversation.
    //
    // Explicit PlayerBots control commands are ignored here
    // so mod-playerbots can continue handling things such as:
    //   invite, follow, stay, attack, etc.
    //
    // Explicit guild-join intent is also ignored because
    // mod-playerbots handles those phrases by sending the
    // genuine guild invitation.
    // -------------------------------------------------
    bool OnPlayerCanUseChat(
        Player* player, uint32 type,
        uint32 language, std::string& msg,
        Player* receiver) override
    {
        if (!sLLMChatterConfig
            || !sLLMChatterConfig->IsEnabled())
        {
            return true;
        }

        if (type != CHAT_MSG_WHISPER)
            return true;

        if (!player
            || !receiver
            || player == receiver)
        {
            return true;
        }

        // Sender must be a real player and recipient must be
        // an actual PlayerBot.
        if (IsPlayerBot(player)
            || !IsPlayerBot(receiver))
        {
            return true;
        }

        // Addon payloads are not conversational speech.
        if (language == LANG_ADDON)
        {
            LogIgnoredAddonChat(
                player, type, msg, "whisper");
            return true;
        }

        if (msg.empty())
            return true;

        std::string safeMsg = msg;

        size_t firstChar =
            safeMsg.find_first_not_of(" \t\n\r");

        if (firstChar == std::string::npos)
            return true;

        if (firstChar > 0)
            safeMsg = safeMsg.substr(firstChar);

        size_t lastChar =
            safeMsg.find_last_not_of(" \t\n\r");

        if (lastChar != std::string::npos)
        {
            safeMsg =
                safeMsg.substr(0, lastChar + 1);
        }

        safeMsg = NormalizeChatTextForDb(
            safeMsg,
            sLLMChatterConfig->_maxMessageLength);

        if (safeMsg.empty())
            return true;

        // Existing PlayerBots commands stay entirely inside
        // mod-playerbots and never consume LLM tokens.
        if (IsLikelyPlayerbotControlCommand(safeMsg))
            return true;

        // Normalize a copy for guild-join intent matching.
        std::string guildIntent = safeMsg;

        std::transform(
            guildIntent.begin(),
            guildIntent.end(),
            guildIntent.begin(),
            [](unsigned char c)
            {
                return static_cast<char>(
                    std::tolower(c));
            });

        while (!guildIntent.empty()
            && std::isspace(
                static_cast<unsigned char>(
                    guildIntent.front())))
        {
            guildIntent.erase(
                guildIntent.begin());
        }

        while (!guildIntent.empty()
            && std::isspace(
                static_cast<unsigned char>(
                    guildIntent.back())))
        {
            guildIntent.pop_back();
        }

        while (!guildIntent.empty()
            && (
                guildIntent.back() == '?'
                || guildIntent.back() == '!'
                || guildIntent.back() == '.'
                || guildIntent.back() == ','
            ))
        {
            guildIntent.pop_back();
        }

        bool wantsGuildInvite =
            guildIntent == "guild invite"
            || guildIntent == "invite guild"
            || guildIntent == "invite to guild"
            || guildIntent == "invite me to guild"
            || guildIntent == "guild invite me"
            || guildIntent == "ginvite"
            || guildIntent == "guild me"
            || guildIntent == "send guild invite"
            || guildIntent == "send me a guild invite"
            || guildIntent == "can i join the guild"
            || guildIntent == "can i join your guild"
            || guildIntent == "i want to join the guild"
            || guildIntent == "i want to join your guild"
            || guildIntent == "i wanna join the guild"
            || guildIntent == "i wanna join your guild"
            || guildIntent == "i'll join"
            || guildIntent == "ill join"
            || guildIntent == "i will join"
            || guildIntent == "i wanna join"
            || guildIntent == "i want to join"
            || guildIntent == "can i join"
            || guildIntent == "let me join"
            || guildIntent == "sign me up"
            || guildIntent == "i'm in"
            || guildIntent == "im in"
            || guildIntent == "count me in";

        if (wantsGuildInvite)
            return true;

        // A bot actively replying to a real player's conversational
        // whisper should no longer present itself as AFK.
        if (receiver->isAFK())
            receiver->ToggleAFK();

        uint32 playerGuid =
            player->GetGUID().GetCounter();

        uint32 botGuid =
            receiver->GetGUID().GetCounter();

        std::string playerName =
            player->GetName();

        std::string botName =
            receiver->GetName();

        std::string extraData =
            "{"
            "\"player_guid\":" +
                std::to_string(playerGuid) + ","
            "\"player_name\":\"" +
                JsonEscape(playerName) + "\","
            "\"player_message\":\"" +
                JsonEscape(safeMsg) + "\","
            "\"bot_guid\":" +
                std::to_string(botGuid) + ","
            "\"bot_name\":\"" +
                JsonEscape(botName) + "\","
            + BuildBotStateJson(receiver)
            + "}";

        extraData = EscapeString(extraData);

        QueueChatterEvent(
            "bot_player_whisper",
            "player",
            receiver->GetZoneId(),
            receiver->GetMapId(),

            // Player-directed whispers should be treated
            // at the same priority level as other direct
            // player-message responses.
            GetChatterEventPriority(
                "guild_player_message"),

            "",
            botGuid,
            botName,
            playerGuid,
            playerName,
            0,
            extraData,
            0,
            120,
            false
        );

        LOG_DEBUG(
            "module",
            "LLMChatter: queued private whisper "
            "player={} bot={} message='{}'",
            playerName,
            botName,
            safeMsg);

        return true;
    }

    bool OnPlayerCanUseChat(
        Player* player, uint32 type,
        uint32 language, std::string& msg,
        Channel* channel) override
    {
        if (!sLLMChatterConfig
            || !sLLMChatterConfig->IsEnabled()
            || !sLLMChatterConfig->_generalChannelEnable
            || !sLLMChatterConfig->_useGeneralChatReact)
            return true;

        if (!channel
            || channel->GetChannelId()
                != ChatChannelId::GENERAL)
            return true;

        if (player)
        {
            Map* gMap = player->GetMap();
            if (gMap
                && (gMap->IsRaid()
                    || gMap->IsBattleground()))
                return true;
        }

        if (!player || IsPlayerBot(player))
            return true;

        // Ignore hidden addon traffic (DBM, Questie, ElvUI, ...);
        // it is real chat tagged LANG_ADDON, not player speech.
        if (language == LANG_ADDON)
        {
            LogIgnoredAddonChat(
                player, type, msg, "general");
            return true;
        }

        if (msg.empty())
            return true;

        {
            bool hasUnderscore = false;
            bool allCapsOrSep = true;
            for (char c : msg)
            {
                if (c == '_')
                    hasUnderscore = true;
                else if (c != ' ' && c != '\t'
                    && c != '\n' && c != '\r'
                    && !(c >= 'A' && c <= 'Z'))
                {
                    allCapsOrSep = false;
                    break;
                }
            }
            if (hasUnderscore && allCapsOrSep)
                return true;
        }

        if (msg.size() > 2 && msg[0] == '|'
            && msg[1] == 'c')
        {
            std::string stripped = msg;
            size_t start, end;
            while ((start = stripped.find("|c"))
                   != std::string::npos
                && (end = stripped.find("|r", start))
                   != std::string::npos)
            {
                stripped.erase(start,
                    end - start + 2);
            }
            stripped.erase(0,
                stripped.find_first_not_of(" \t"));
            if (!stripped.empty())
            {
                stripped.erase(
                    stripped.find_last_not_of(
                        " \t") + 1);
            }
            if (stripped.empty())
                return true;
        }

        std::string safeMsg = msg;
        size_t firstChar =
            safeMsg.find_first_not_of(" \t\n\r");
        if (firstChar == std::string::npos)
            return true;
        if (firstChar > 0)
            safeMsg = safeMsg.substr(firstChar);
        size_t lastChar =
            safeMsg.find_last_not_of(" \t\n\r");
        if (lastChar != std::string::npos)
            safeMsg =
                safeMsg.substr(0, lastChar + 1);
        if (safeMsg.empty())
            return true;
        // UTF-8 safe clamp: never split a multi-byte char
        safeMsg = NormalizeChatTextForDb(
            safeMsg, sLLMChatterConfig->_maxMessageLength);
        // Normalization can strip an all-invalid payload
        // (e.g. \xFF\xFF...) down to empty — drop it.
        if (safeMsg.empty())
            return true;

        uint32 zoneId = player->GetZoneId();
        std::string playerName = player->GetName();

        CharacterDatabase.Execute(
            "INSERT INTO llm_general_chat_history "
            "(zone_id, speaker_name, is_bot, message)"
            " VALUES ({}, '{}', 0, '{}')",
            zoneId,
            EscapeString(playerName),
            EscapeString(safeMsg));

        CharacterDatabase.Execute(
            "DELETE FROM llm_general_chat_history "
            "WHERE zone_id = {} AND id NOT IN "
            "(SELECT id FROM (SELECT id FROM "
            "llm_general_chat_history "
            "WHERE zone_id = {} "
            "ORDER BY id DESC LIMIT "
            + std::to_string(
                sLLMChatterConfig
                    ->_generalChatHistoryLimit)
            + ") AS keep)",
            zoneId, zoneId);

        time_t now = time(nullptr);

        // Mage General services are intentionally disabled for now.
        // Keep the recognition helper available for future work, but
        // do not classify, advertise, reserve, or execute Mage services.
        bool possibleMageService = false;

        uint32 linkedLockboxEntry = 0;

        bool possibleLockpickService =
            LooksLikeGeneralLockpickServiceRequest(
                safeMsg,
                linkedLockboxEntry);

        bool serviceAllowed =
            possibleMageService
            || possibleLockpickService;

        if (serviceAllowed)
        {
            std::lock_guard<std::mutex> guard(
                _generalServiceCooldownsMutex);

            uint32 playerGuid =
                player->GetGUID().GetCounter();

            auto it =
                _generalServiceCooldowns.find(
                    playerGuid);

            if (
                it != _generalServiceCooldowns.end()
                && (now - it->second)
                    < GENERAL_SERVICE_COOLDOWN_SECONDS)
            {
                serviceAllowed = false;
            }
        }

        bool ambientAllowed = true;

        {
            std::lock_guard<std::mutex> guard(
                _generalChatCooldownsMutex);

            auto it =
                _generalChatCooldowns.find(zoneId);

            if (
                it != _generalChatCooldowns.end()
                && (now - it->second)
                   < (time_t)sLLMChatterConfig
                       ->_generalChatCooldown)
            {
                ambientAllowed = false;
            }
        }

        bool isQuestion =
            !safeMsg.empty()
            && safeMsg.back() == '?';

        uint32 chance = isQuestion
            ? sLLMChatterConfig
                ->_generalChatQuestionChance
            : sLLMChatterConfig
                ->_generalChatChance;

        if (
            ambientAllowed
            && urand(1, 100) > chance)
        {
            ambientAllowed = false;
        }

        // A service-like request may bypass ambient RNG/cooldown.
        // Everything else preserves the old early-return behavior.
        if (!serviceAllowed && !ambientAllowed)
            return true;

        std::string zoneName = GetZoneName(zoneId);
        if (zoneName.empty())
            zoneName = "Unknown";

        std::vector<Player*> zoneBots;
        zoneBots.reserve(8);

        {
            WorldSessionMgr::SessionMap const& sessions =
                sWorldSessionMgr->GetAllSessions();
            for (auto const& pair : sessions)
            {
                WorldSession* session = pair.second;
                if (!session)
                    continue;
                Player* p = session->GetPlayer();
                if (!p || !p->IsInWorld())
                    continue;
                if (!IsPlayerBot(p))
                    continue;
                if (p->GetZoneId() != zoneId)
                    continue;
                zoneBots.push_back(p);

                if (
                    !serviceAllowed
                    && zoneBots.size()
                        >= sLLMChatterConfig
                            ->_maxBotsPerZone)
                {
                    break;
                }
            }
        }

        if (
            serviceAllowed
            || zoneBots.size()
                < sLLMChatterConfig->_maxBotsPerZone)
        {
            auto allBots =
                sRandomPlayerbotMgr.GetAllBots();
            for (auto& pair : allBots)
            {
                Player* bot = pair.second;
                if (!bot || !bot->IsInWorld())
                    continue;
                if (bot->GetZoneId() != zoneId)
                    continue;

                bool found = false;
                for (Player* b : zoneBots)
                {
                    if (b->GetGUID() == bot->GetGUID())
                    {
                        found = true;
                        break;
                    }
                }
                if (!found)
                {
                    zoneBots.push_back(bot);

                    if (
                        !serviceAllowed
                        && zoneBots.size()
                            >= sLLMChatterConfig
                                ->_maxBotsPerZone)
                    {
                        break;
                    }
                }
            }
        }

        zoneBots.erase(
            std::remove_if(
                zoneBots.begin(), zoneBots.end(),
                [](Player* b) {
                    return !CanSpeakInGeneralChannel(b);
                }),
            zoneBots.end());

        if (zoneBots.empty())
            return true;

        if (
            serviceAllowed
            && possibleLockpickService)
        {
            std::vector<Player*> rogueBots;
            std::vector<Player*> sameAreaRogues;

            rogueBots.reserve(
                zoneBots.size());

            sameAreaRogues.reserve(
                zoneBots.size());

            uint32 playerAreaId =
                player->GetAreaId();

            uint32 requiredSkill = 0;

            if (
                GetLLMLockboxRequiredSkill(
                    linkedLockboxEntry,
                    requiredSkill))
            {
                for (Player* bot : zoneBots)
                {
                    if (
                        !bot
                        || !bot->IsInWorld()
                        || !bot->IsAlive()
                        || bot->getClass()
                            != CLASS_ROGUE
                        || bot->GetTeamId()
                            != player->GetTeamId()
                        || !CanLLMServiceBotOpenLockbox(
                            bot,
                            linkedLockboxEntry))
                    {
                        continue;
                    }

                    rogueBots.push_back(
                        bot);

                    if (
                        bot->GetAreaId()
                        == playerAreaId)
                    {
                        sameAreaRogues.push_back(
                            bot);
                    }
                }

                if (!sameAreaRogues.empty())
                {
                    rogueBots =
                        sameAreaRogues;
                }

                std::sort(
                    rogueBots.begin(),
                    rogueBots.end(),
                    [](Player* a, Player* b)
                    {
                        return
                            a->GetGUID().GetCounter()
                            < b->GetGUID().GetCounter();
                    });

                if (!rogueBots.empty())
                {
                    std::string botGuids = "[";
                    std::string botNames = "[";
                    std::string botStates = "{";

                    for (
                        size_t i = 0;
                        i < rogueBots.size();
                        ++i)
                    {
                        Player* rogue =
                            rogueBots[i];

                        if (i > 0)
                        {
                            botGuids += ",";
                            botNames += ",";
                            botStates += ",";
                        }

                        uint32 rogueGuid =
                            rogue->GetGUID()
                                .GetCounter();

                        botGuids +=
                            std::to_string(
                                rogueGuid);

                        botNames += "\"" +
                            JsonEscape(
                                rogue->GetName())
                            + "\"";

                        botStates += "\"" +
                            std::to_string(
                                rogueGuid)
                            + "\":{";

                        botStates +=
                            BuildBotStateJson(
                                rogue);

                        botStates += "}";
                    }

                    botGuids += "]";
                    botNames += "]";
                    botStates += "}";

                    std::string serviceExtraData =
                        "{"
                        "\"service_hint\":\"lockpick\","
                        "\"item_entry\":" +
                        std::to_string(
                            linkedLockboxEntry) + ","
                        "\"required_skill\":" +
                        std::to_string(
                            requiredSkill) + ","
                        "\"player_name\":\"" +
                        JsonEscape(
                            playerName) + "\","
                        "\"player_gender\":" +
                        std::to_string(
                            player->getGender()) + ","
                        "\"player_message\":\"" +
                        JsonEscape(
                            safeMsg) + "\","
                        "\"zone_id\":" +
                        std::to_string(
                            zoneId) + ","
                        "\"zone_name\":\"" +
                        JsonEscape(
                            zoneName) + "\","
                        "\"player_area_id\":" +
                        std::to_string(
                            playerAreaId) + ","
                        "\"bot_guids\":" +
                        botGuids + ","
                        "\"bot_names\":" +
                        botNames + ","
                        "\"bot_states\":" +
                        botStates +
                        "}";

                    serviceExtraData =
                        EscapeString(
                            serviceExtraData);

                    QueueChatterEvent(
                        "player_general_service_request",
                        "zone",
                        zoneId,
                        player->GetMapId(),
                        GetChatterEventPriority(
                            "player_general_msg"),
                        "general_service:" +
                            std::to_string(
                                player->GetGUID()
                                    .GetCounter()),
                        player->GetGUID()
                            .GetCounter(),
                        playerName,
                        0,
                        "",
                        0,
                        serviceExtraData,
                        GetReactionDelaySeconds(
                            "player_general_msg"),
                        60,
                        false
                    );

                    {
                        std::lock_guard<std::mutex>
                            guard(
                                _generalServiceCooldownsMutex);

                        _generalServiceCooldowns[
                            player->GetGUID()
                                .GetCounter()
                        ] = now;
                    }

                    return true;
                }
            }
        }

        if (
            serviceAllowed
            && possibleMageService)
        {
            std::vector<Player*> mageBots;
            std::vector<Player*> sameAreaMages;

            mageBots.reserve(zoneBots.size());
            sameAreaMages.reserve(zoneBots.size());

            uint32 playerAreaId =
                player->GetAreaId();

            for (Player* bot : zoneBots)
            {
                if (
                    !bot
                    || !bot->IsInWorld()
                    || !bot->IsAlive()
                    || bot->getClass() != CLASS_MAGE
                    || bot->GetTeamId()
                        != player->GetTeamId())
                {
                    continue;
                }

                mageBots.push_back(bot);

                if (
                    bot->GetAreaId()
                    == playerAreaId)
                {
                    sameAreaMages.push_back(bot);
                }
            }

            // Prefer a Mage physically in the same area/subzone,
            // but allow another Mage elsewhere in the same zone.
            if (!sameAreaMages.empty())
                mageBots = sameAreaMages;

            if (!mageBots.empty())
            {
                std::sort(
                    mageBots.begin(),
                    mageBots.end(),
                    [](Player* a, Player* b)
                    {
                        return
                            a->GetGUID().GetCounter()
                            < b->GetGUID().GetCounter();
                    });

                std::string mageGuids = "[";
                std::string mageNames = "[";
                std::string mageStates = "{";

                for (
                    size_t i = 0;
                    i < mageBots.size();
                    ++i)
                {
                    Player* mage = mageBots[i];

                    if (i > 0)
                    {
                        mageGuids += ",";
                        mageNames += ",";
                        mageStates += ",";
                    }

                    uint32 mageGuid =
                        mage->GetGUID().GetCounter();

                    mageGuids +=
                        std::to_string(mageGuid);

                    mageNames += "\"" +
                        JsonEscape(
                            mage->GetName())
                        + "\"";

                    mageStates += "\"" +
                        std::to_string(mageGuid)
                        + "\":{";

                    mageStates +=
                        BuildBotStateJson(mage);

                    mageStates += "}";
                }

                mageGuids += "]";
                mageNames += "]";
                mageStates += "}";

                std::string serviceExtraData = "{"
                    "\"player_name\":\"" +
                        JsonEscape(playerName) + "\","
                    "\"player_gender\":" +
                        std::to_string(
                            player->getGender()) + ","
                    "\"player_message\":\"" +
                        JsonEscape(safeMsg) + "\","
                    "\"zone_id\":" +
                        std::to_string(zoneId) + ","
                    "\"zone_name\":\"" +
                        JsonEscape(zoneName) + "\","
                    "\"player_area_id\":" +
                        std::to_string(playerAreaId) + ","
                    "\"bot_guids\":" +
                        mageGuids + ","
                    "\"bot_names\":" +
                        mageNames + ","
                    "\"bot_states\":" +
                        mageStates +
                    "}";

                serviceExtraData =
                    EscapeString(
                        serviceExtraData);

                QueueChatterEvent(
                    "player_general_service_request",
                    "zone",
                    zoneId,
                    player->GetMapId(),
                    GetChatterEventPriority(
                        "player_general_msg"),
                    "general_service:" +
                        std::to_string(
                            player->GetGUID()
                                .GetCounter()),
                    player->GetGUID().GetCounter(),
                    playerName,
                    0,
                    "",
                    0,
                    serviceExtraData,
                    GetReactionDelaySeconds(
                        "player_general_msg"),
                    60,
                    false
                );

                {
                    std::lock_guard<std::mutex> guard(
                        _generalServiceCooldownsMutex);

                    _generalServiceCooldowns[
                        player->GetGUID()
                            .GetCounter()
                    ] = now;
                }

                return true;
            }
        }

        // A service-looking message with no eligible Mage must
        // not bypass the ordinary General response probability.
        if (!ambientAllowed)
            return true;

        // Preserve the original second cooldown check/commit.
        // Service discovery never writes this cooldown.
        {
            std::lock_guard<std::mutex> guard(
                _generalChatCooldownsMutex);

            auto it =
                _generalChatCooldowns.find(zoneId);

            if (
                it != _generalChatCooldowns.end()
                && (now - it->second)
                   < (time_t)sLLMChatterConfig
                       ->_generalChatCooldown)
            {
                return true;
            }

            _generalChatCooldowns[zoneId] = now;
        }

        // A service pre-gate may have collected more bots than
        // ordinary General allows. If no Mage was found and we
        // are falling back to ambient chatter, restore the old
        // candidate cap before normal selection.
        if (
            zoneBots.size()
            > sLLMChatterConfig->_maxBotsPerZone)
        {
            std::shuffle(
                zoneBots.begin(),
                zoneBots.end(),
                std::mt19937{
                    std::random_device{}()
                });

            zoneBots.resize(
                sLLMChatterConfig
                    ->_maxBotsPerZone);
        }

        std::shuffle(
            zoneBots.begin(), zoneBots.end(),
            std::mt19937{std::random_device{}()});
        uint32 pickCount = zoneBots.size();

        std::string botGuids = "[";
        std::string botNames = "[";
        std::string botStates = "{";

        for (uint32 i = 0; i < pickCount; ++i)
        {
            Player* bot = zoneBots[i];

            if (i > 0)
            {
                botGuids += ",";
                botNames += ",";
                botStates += ",";
            }

            uint32 botGuid =
                bot->GetGUID().GetCounter();

            botGuids += std::to_string(botGuid);

            botNames += "\"" +
                JsonEscape(bot->GetName()) + "\"";

            // Capture the authoritative live PlayerBots
            // state at the exact moment the real player
            // sends the General message.
            botStates += "\"" +
                std::to_string(botGuid) + "\":{";
            botStates += BuildBotStateJson(bot);
            botStates += "}";
        }

        botGuids += "]";
        botNames += "]";
        botStates += "}";

        std::string extraData = "{"
            "\"player_name\":\"" +
                JsonEscape(playerName) + "\","
            "\"player_gender\":" +
                std::to_string(player->getGender()) + ","
            "\"player_message\":\"" +
                JsonEscape(safeMsg) + "\","
            "\"zone_id\":" +
                std::to_string(zoneId) + ","
            "\"zone_name\":\"" +
                JsonEscape(zoneName) + "\","
            "\"bot_guids\":" + botGuids + ","
            "\"bot_names\":" + botNames + ","
            "\"bot_states\":" + botStates +
            "}";

        extraData = EscapeString(extraData);

        QueueChatterEvent(
            "player_general_msg",
            "zone",
            zoneId,
            player->GetMapId(),
            GetChatterEventPriority(
                "player_general_msg"),
            "general_chat:" +
                std::to_string(zoneId),
            player->GetGUID().GetCounter(),
            playerName,
            0,
            "",
            0,
            extraData,
            GetReactionDelaySeconds(
                "player_general_msg"),
            120,
            false
        );

        return true;
    }

    void OnPlayerUpdate(
        Player* player, uint32 /*p_time*/) override
    {
        if (!sLLMChatterConfig
            || !sLLMChatterConfig->IsEnabled())
            return;

        RefreshGroupBotTravelStates(player);
    }

    void OnPlayerPVPKill(
        Player* killer, Player* killed) override
    {
        if (!sLLMChatterConfig
            || !sLLMChatterConfig->IsEnabled()
            || !sLLMChatterConfig->_bgChatterEnable)
            return;
        if (!killer || !killed)
            return;

        if (!killer->InBattleground())
            return;
        Battleground* bg = killer->GetBattleground();
        if (!bg || !bg->isBattleground())
            return;

        if (urand(1, 100)
            > sLLMChatterConfig->_eventReactionChance)
            return;

        std::string extraBase = "{"
            "\"victim_name\":\"" +
                JsonEscape(killed->GetName()) +
                "\","
            "\"victim_class\":" +
                std::to_string(
                    killed->getClass()) +
            ",\"killer_name\":\"" +
                JsonEscape(killer->GetName()) +
                "\","
            "\"killer_is_real_player\":"
                + std::string(
                    IsPlayerBot(killer)
                    ? "false" : "true") +
            "}";

        for (auto const& [g, p] : bg->GetPlayers())
        {
            Player* rp =
                ObjectAccessor::FindPlayer(g);
            if (!rp || IsPlayerBot(rp))
                continue;
            if (rp->GetBgTeamId()
                != killer->GetBgTeamId())
                continue;

            Group* group = rp->GetGroup();
            if (!group || !GroupHasBots(group))
                continue;

            std::string extra = extraBase;
            AppendBGContext(bg, rp, extra);
            QueueBGEvent(rp, "bg_pvp_kill", extra);
        }

    }

    void OnPlayerUpdateArea(
        Player* player, uint32 oldArea,
        uint32 newArea) override
    {
        if (!sLLMChatterConfig
            || !sLLMChatterConfig->IsEnabled())
            return;

        if (!player || IsPlayerBot(player))
            return;

        Group* grp = player->GetGroup();
        if (!grp || !GroupHasBots(grp))
            return;

        uint32 gId =
            grp->GetGUID().GetCounter();

        // Always update the area column
        CharacterDatabase.Execute(
            "UPDATE llm_group_bot_traits "
            "SET area = {} "
            "WHERE group_id = {}",
            newArea, gId);
        RefreshGroupBotTravelStates(player, true);

        // Skip if area didn't actually change
        // (can happen on login/teleport)
        if (oldArea == newArea || !newArea)
            return;

        // Skip if the zone also changed — the
        // zone transition handler covers that
        uint32 curZone = player->GetZoneId();
        AreaTableEntry const* oldEntry =
            sAreaTableStore.LookupEntry(oldArea);
        uint32 oldZone = 0;
        if (oldEntry)
            oldZone = oldEntry->zone
                ? oldEntry->zone : oldArea;
        if (oldZone != curZone)
            return;

        uint32 subzoneCooldown =
            sLLMChatterConfig->_groupZoneCooldown;
        uint32 subzoneChance =
            sLLMChatterConfig->_groupZoneChance;

        // Per-group cooldown (shared across all
        // subzones so rapid area crossings don't
        // spam the party channel).
        time_t now = time(nullptr);
        {
            std::lock_guard<std::mutex> guard(
                _subzoneCooldownMutex);
            auto it =
                _subzoneCommentCooldowns.find(gId);
            if (it != _subzoneCommentCooldowns.end()
                && (now - it->second)
                   < static_cast<time_t>(
                         subzoneCooldown))
                return;
        }

        if (urand(1, 100) > subzoneChance)
            return;

        // Resolve area name
        std::string areaName;
        AreaTableEntry const* areaEntry =
            sAreaTableStore.LookupEntry(newArea);
        if (areaEntry)
        {
            uint8 loc =
                sWorld->GetDefaultDbcLocale();
            char const* n =
                areaEntry->area_name[loc];
            areaName = n ? n : "";
            if (areaName.empty())
            {
                n = areaEntry
                    ->area_name[LOCALE_enUS];
                areaName = n ? n : "";
            }
        }
        if (areaName.empty())
            return;

        // Pick a random alive bot from the group
        std::vector<Player*> aliveBots;
        for (auto const& ref :
            grp->GetMemberSlots())
        {
            Player* m =
                ObjectAccessor::FindPlayer(
                    ref.guid);
            if (m && IsPlayerBot(m)
                && m->IsAlive())
                aliveBots.push_back(m);
        }
        if (aliveBots.empty())
            return;
        Player* bot = aliveBots[
            urand(0, aliveBots.size() - 1)];

        // Resolve zone name
        std::string zoneName;
        AreaTableEntry const* zoneEntry =
            sAreaTableStore.LookupEntry(curZone);
        if (zoneEntry)
        {
            uint8 loc =
                sWorld->GetDefaultDbcLocale();
            char const* n =
                zoneEntry->area_name[loc];
            zoneName = n ? n : "";
            if (zoneName.empty())
            {
                n = zoneEntry
                    ->area_name[LOCALE_enUS];
                zoneName = n ? n : "";
            }
        }

        uint32 botGuid =
            bot->GetGUID().GetCounter();

        std::string extraData = "{"
            "\"bot_guid\":" +
                std::to_string(botGuid) + ","
            "\"bot_name\":\"" +
                JsonEscape(bot->GetName()) + "\","
            "\"bot_class\":" +
                std::to_string(
                    bot->getClass()) + ","
            "\"bot_race\":" +
                std::to_string(
                    bot->getRace()) + ","
            "\"bot_gender\":" +
                std::to_string(
                    bot->getGender()) + ","
            "\"bot_level\":" +
                std::to_string(
                    bot->GetLevel()) + ","
            + BuildBotStateJson(bot) + ","
            "\"group_id\":" +
                std::to_string(gId) + ","
            "\"zone_id\":" +
                std::to_string(curZone) + ","
            "\"zone_name\":\"" +
                JsonEscape(zoneName) + "\","
            "\"area_id\":" +
                std::to_string(newArea) + ","
            "\"area_name\":\"" +
                JsonEscape(areaName) + "\""
            "}";

        extraData = EscapeString(extraData);

        // Stamp cooldown only after all guards pass
        // and the configured RNG succeeded.
        {
            std::lock_guard<std::mutex> guard(
                _subzoneCooldownMutex);
            _subzoneCommentCooldowns[gId] = now;
        }

        QueueChatterEvent(
            "bot_group_subzone_change",
            "player",
            curZone,
            player->GetMapId(),
            GetChatterEventPriority(
                "bot_group_subzone_change"),
            "",
            botGuid,
            bot->GetName(),
            0,
            areaName,
            0,
            extraData,
            GetReactionDelaySeconds(
                "bot_group_subzone_change"),
            120,
            false
        );
    }

    void OnPlayerUpdateZone(
        Player* player, uint32 newZone,
        uint32 newArea) override
    {
        if (!sLLMChatterConfig
            || !sLLMChatterConfig->IsEnabled())
            return;

        if (!player)
            return;

        HandleAmbientPlayerUpdateZone(
            player, newZone);

        // Eviction sweep + cleanup under mutex
        {
            std::lock_guard<std::mutex> guard(
                _intrusionMutex);

            // Eviction sweep (every 300s, TTL 1800s)
            time_t now = time(nullptr);
            if (now - _lastIntrusionEviction > 300)
            {
                _lastIntrusionEviction = now;
                auto it =
                    _intrusionStates.begin();
                while (it
                    != _intrusionStates.end())
                {
                    if (now - it->second.firstSeen
                        > 1800)
                        it = _intrusionStates
                            .erase(it);
                    else
                        ++it;
                }
                auto zt =
                    _zoneAlertThrottle.begin();
                while (zt
                    != _zoneAlertThrottle.end())
                {
                    if (now - zt->second > 1800)
                        zt = _zoneAlertThrottle
                            .erase(zt);
                    else
                        ++zt;
                }
            }

            // Clean up intrusion state for ALL
            // players on zone change
            CleanupIntrusionStateOnZoneChange(
                player->GetGUID().GetCounter(),
                newZone);
        }

        if (!IsPlayerBot(player))
        {
            // Immediately persist zone + map so the
            // Python bridge sees fresh data without
            // waiting for the 15-min autosave.
            CharacterDatabase.Execute(
                "UPDATE characters "
                "SET zone = {}, map = {} "
                "WHERE guid = {}",
                newZone,
                player->GetMapId(),
                player->GetGUID().GetCounter());

            // Real player: update all bot zones
            // in group + check for enemy zone
            HandleGroupPlayerUpdateZone(
                player, newZone, newArea);
            RefreshGroupBotTravelStates(player, true);
            HandleEnemyZoneIntrusion(
                player, newZone);
            return;
        }

        // Bot paths
        EnsureBotInGeneralChannel(player);
        HandleGroupPlayerUpdateZone(
            player, newZone, newArea);
        RefreshGroupBotTravelStates(player, true);
        HandleBotEntersEnemyTerritory(
            player, newZone);
    }
};

void AddLLMChatterPlayerScripts()
{
    new LLMChatterPlayerScript();
}
