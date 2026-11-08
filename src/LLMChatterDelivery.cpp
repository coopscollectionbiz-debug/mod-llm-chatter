/*
 * mod-llm-chatter - outbound message delivery ownership
 */

#include "LLMChatterConfig.h"
#include "Guild.h"
#include "LLMChatterBossDialogue.h"
#include "LLMChatterDelivery.h"
#include "LLMChatterDeliveryBatch.h"
#include "LLMChatterDeliveryPolicy.h"
#include "LLMChatterGuild.h"
#include "LLMChatterLiveContext.h"
#include "LLMChatterProximity.h"
#include "LLMChatterShared.h"
#include "LLMChatterWhisper.h"

#include "Channel.h"
#include "ChannelMgr.h"
#include "Chat.h"
#include "Creature.h"
#include "DatabaseEnv.h"
#include "DBCStores.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "Playerbots.h"
#include "SocialMgr.h"
#include "StringFormat.h"
#include "Timer.h"
#include "World.h"
#include "WorldSession.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <ctime>
#include <limits>
#include <map>
#include <unordered_set>
#include <vector>

namespace
{
using SceneFacingKey = std::pair<uint32, uint32>;
struct SceneFacingState
{
    float orientation;
    time_t updatedAt;
    uint32 playerGuid;
};
static std::map<SceneFacingKey, SceneFacingState>
    _sceneOriginalFacings;

class DelayedNPCFacingResetEvent : public BasicEvent
{
public:
    DelayedNPCFacingResetEvent(
        ObjectGuid playerGuid, uint32 spawnId,
        float orientation)
        : _playerGuid(playerGuid)
        , _spawnId(spawnId)
        , _orientation(orientation)
    {
    }

    bool Execute(uint64 /*time*/,
                 uint32 /*diff*/) override
    {
        Player* player =
            ObjectAccessor::FindConnectedPlayer(
                _playerGuid);
        if (!player || !player->IsInWorld())
            return true;

        Creature* creature = FindCreatureBySpawnId(
            player->GetMap(), _spawnId);
        if (!creature || !creature->IsAlive()
            || creature->IsInCombat()
            || !IsSafeForChatterFacing(creature))
            return true;

        creature->SetFacingTo(_orientation);
        return true;
    }

private:
    ObjectGuid _playerGuid;
    uint32 _spawnId;
    float _orientation;
};

void ScheduleSceneFacingResets(
    uint32 eventId, uint32 delaySeconds)
{
    if (!eventId)
        return;

    for (auto it = _sceneOriginalFacings.begin();
         it != _sceneOriginalFacings.end();)
    {
        if (it->first.first != eventId)
        {
            ++it;
            continue;
        }

        uint32 spawnId = it->first.second;
        SceneFacingState const state = it->second;
        ObjectGuid playerGuid =
            ObjectGuid::Create<HighGuid::Player>(
                state.playerGuid);
        Player* player =
            ObjectAccessor::FindConnectedPlayer(
                playerGuid);
        Creature* creature = player && player->IsInWorld()
            ? FindCreatureBySpawnId(
                player->GetMap(), spawnId)
            : nullptr;
        if (creature && creature->IsInWorld())
        {
            creature->m_Events.AddEvent(
                new DelayedNPCFacingResetEvent(
                    playerGuid, spawnId,
                    state.orientation),
                creature->m_Events.CalculateTime(
                    delaySeconds * IN_MILLISECONDS));
        }
        it = _sceneOriginalFacings.erase(it);
    }
}

uint32 ExtractJsonUInt(
    std::string const& json, char const* key)
{
    if (!key || !*key)
        return 0;

    std::string marker = std::string("\"")
        + key + "\":";
    size_t pos = json.find(marker);
    if (pos == std::string::npos)
        return 0;
    pos += marker.size();
    while (pos < json.size()
        && std::isspace(
            static_cast<unsigned char>(json[pos])))
    {
        ++pos;
    }

    uint64 value = 0;
    bool foundDigit = false;
    while (pos < json.size()
        && std::isdigit(
            static_cast<unsigned char>(json[pos])))
    {
        foundDigit = true;
        value = value * 10
            + static_cast<uint64>(json[pos] - '0');
        if (value > std::numeric_limits<uint32>::max())
            return 0;
        ++pos;
    }
    return foundDigit ? static_cast<uint32>(value) : 0;
}

bool HasNonEmptyJsonString(
    std::string const& json, char const* key)
{
    if (!key || !*key)
        return false;

    std::string marker = std::string("\"")
        + key + "\":";
    size_t pos = json.find(marker);
    if (pos == std::string::npos)
        return false;
    pos += marker.size();
    while (pos < json.size()
        && std::isspace(
            static_cast<unsigned char>(json[pos])))
    {
        ++pos;
    }
    if (pos >= json.size() || json[pos] != '"')
        return false;
    ++pos;
    return pos < json.size() && json[pos] != '"';
}

bool IsDirectedProximityEvent(
    std::string const& eventType)
{
    return eventType == "proximity_player_say"
        || eventType == "proximity_player_conversation"
        || eventType == "proximity_player_emote";
}

bool IsFactionBoundReplyEvent(
    std::string const& eventType)
{
    return eventType == "player_general_msg"
        || eventType == "bot_group_player_msg"
        || eventType == "guild_player_message"
        || eventType == "guild_login_greeting";
}

// Rows whose terminal write is queued but possibly not yet applied.
// Delivered and dropped rows are held well past any async write lag;
// retried rows only until their rescheduled deliver_at.
constexpr int64 HANDLED_ROW_HOLD_SECONDS = 300;
constexpr int64 RETRY_ROW_HOLD_SECONDS = 4;
// A fetch that has not returned after this long is reported once.
constexpr uint32 FETCH_STALL_WARN_MS = 15000;

LLMChatterDeliveryBatch::HandledRowGuard _handledRows;
LLMChatterDeliveryBatch::CancelledConversationGuard
    _cancelledConversations;

struct DeliveryDiagnostics
{
    uint64 fetches = 0;
    uint64 rowsFetched = 0;
    uint64 batches = 0;
    uint64 selected = 0;
    uint32 maxBatch = 0;
    uint64 delivered = 0;
    uint64 retried = 0;
    uint64 deferred = 0;
    uint64 guardSkipped = 0;
    uint64 fetchLatencyTotalMs = 0;
    uint32 fetchLatencyMaxMs = 0;
    uint64 callbackTotalUs = 0;
    uint32 callbackMaxUs = 0;
    std::map<std::string, uint64> dropped;
};

DeliveryDiagnostics _diag;
bool _fetchInFlight = false;
uint32 _fetchIssuedMs = 0;
bool _fetchStallWarned = false;
time_t _lastExpireSweep = 0;
time_t _lastDiagnosticsReport = 0;
bool _diagnosticsQueryInFlight = false;

void CountDrop(char const* reason)
{
    ++_diag.dropped[reason ? reason : "delivery_failed"];
}

void FinalizeDroppedMessage(
    uint32 messageId,
    uint32 eventId,
    uint32 sequence,
    std::string const& eventType,
    char const* reason)
{
    char const* finalReason =
        reason ? reason : "delivery_failed";
    CountDrop(finalReason);
    _handledRows.Mark(
        messageId, time(nullptr), HANDLED_ROW_HOLD_SECONDS);
    ExecuteLLMChatterWrite(Acore::StringFormat(
        "UPDATE llm_chatter_messages "
        "SET delivered = 1, delivered_at = NOW(), "
        "drop_reason = '{}' WHERE id = {}",
        EscapeString(finalReason),
        messageId));

    if (!eventId || !IsDirectedProximityEvent(eventType))
        return;

    // The cancellation write may still be queued when the next fetch
    // runs, so later lines are also blocked in memory.
    _cancelledConversations.Cancel(
        eventId, sequence,
        time(nullptr) + HANDLED_ROW_HOLD_SECONDS);
    ExecuteLLMChatterWrite(Acore::StringFormat(
        "UPDATE llm_chatter_messages "
        "SET delivered = 1, delivered_at = NOW(), "
        "drop_reason = 'cancelled_after_directed_drop' "
        "WHERE event_id = {} AND sequence > {} "
        "AND delivered = 0",
        eventId, sequence));

    ScheduleSceneFacingResets(
        eventId,
        sLLMChatterConfig
            ? sLLMChatterConfig
                  ->_proxChatterFacingResetDelay
            : 0);
}

// One fetched llm_chatter_messages row plus the event and sequence
// context that the legacy path looked up with extra blocking queries.
struct DeliveryRow
{
    uint32 messageId = 0;
    uint32 botGuid = 0;
    std::string botName;
    std::string message;
    std::string channel;
    std::string emoteName;
    uint32 npcSpawnId = 0;
    uint32 playerGuid = 0;
    uint32 sequence = 0;
    uint32 eventId = 0;
    uint32 eventZoneId = 0;
    uint32 groupId = 0;
    std::string deliveryPolicy;
    std::string deliveryReason;
    std::string ownerSubsystem;
    uint32 addresseePlayerGuid = 0;
    uint32 addresseeBotGuid = 0;
    uint32 addresseeNPCSpawnId = 0;
    bool hasEventMapId = false;
    uint32 eventMapId = 0;
    std::string eventExtraData;
    std::string eventType;
    uint32 eventSubjectGuid = 0;
    uint32 queueId = 0;
    uint32 targetEntry = 0;
    uint32 targetGuid = 0;
    uint32 prevBotGuid = 0;
    uint32 prevNpcSpawnId = 0;
    bool hasLaterUndelivered = false;
    int64 remainingUs = -1;
    std::string reactiveAudience;
    std::string reactiveTurn;
    std::string claimJson;
};

enum class DeliveryOutcome
{
    // Spoken; caller records delivered_at.
    Sent,
    // Lifetime elapsed while preparing the row; no speech/bookkeeping.
    Expired,
    // Not spoken and not retryable; caller records the drop reason.
    Dropped,
    // Transient failure; caller reschedules the row.
    Retry,
    // The row already wrote its own terminal state.
    Handled,
};

template <typename T>
T FieldOr(Field const& field, T def)
{
    return field.IsNull() ? def : field.Get<T>();
}
} // namespace

// Speaks one fetched row on the world thread. Game objects are
// resolved here, never in a database thread.
static DeliveryOutcome DeliverRow(
    DeliveryRow const& row, time_t now, std::string& dropReasonOut,
    std::chrono::steady_clock::time_point issuedAt)
{
    auto const withinDeadline = [&row, issuedAt]()
    {
        int64 const elapsedUs = std::chrono::ceil<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - issuedAt).count();
        return LLMChatterDeliveryPolicy::CheckDispatch(row.remainingUs, elapsedUs, true)
            == LLMChatterDeliveryPolicy::DispatchDecision::Send;
    };
    uint32 const messageId = row.messageId;
    uint32 const botGuid = row.botGuid;
    std::string const& botName = row.botName;
    std::string const& message = row.message;
    std::string const& channel = row.channel;
    std::string const& emoteName = row.emoteName;
    uint32 const npcSpawnId = row.npcSpawnId;
    uint32 const playerGuid = row.playerGuid;
    uint32 const sequence = row.sequence;
    uint32 const eventId = row.eventId;
    uint32 const eventZoneId = row.eventZoneId;
    uint32 const groupId = row.groupId;
    std::string const& deliveryPolicy = row.deliveryPolicy;
    std::string const& deliveryReason = row.deliveryReason;
    std::string const& ownerSubsystem = row.ownerSubsystem;
    uint32 const addresseePlayerGuid = row.addresseePlayerGuid;
    uint32 const addresseeBotGuid = row.addresseeBotGuid;
    uint32 const addresseeNPCSpawnId = row.addresseeNPCSpawnId;
    bool const hasEventMapId = row.hasEventMapId;
    uint32 const eventMapId = row.eventMapId;
    std::string const& eventExtraData = row.eventExtraData;
    uint32 eventInstanceId =
        ExtractJsonUInt(
            eventExtraData, "instance_id");
    std::string const& eventType = row.eventType;
    uint32 const eventSubjectGuid = row.eventSubjectGuid;

    using LLMChatterDeliveryPolicy::ReactiveRoute;
    auto const route = LLMChatterDeliveryPolicy::ClassifyReactiveRoute(eventType, deliveryReason, channel,
        eventId && eventSubjectGuid && !row.reactiveAudience.empty() && !row.reactiveTurn.empty());
    if (route == ReactiveRoute::DropRelay || route == ReactiveRoute::DropUnscoped)
    {
        dropReasonOut = route == ReactiveRoute::DropRelay ? "general_party_relay_disabled" : "unscoped_reactive_reply";
        return DeliveryOutcome::Dropped;
    }
    bool const reactiveParty = route == ReactiveRoute::Party;
    bool const reactiveGeneral = route == ReactiveRoute::General;
    bool const reactiveWhisper = route == ReactiveRoute::Whisper;

    // Master General-channel toggle. If General chatter is
    // disabled, deliberately consume any already-queued General
    // rows instead of speaking them. The row was claimed
    // (delivered = 1) above, so returning here drops it without
    // retry -- flipping LLMChatter.GeneralChannel.Enable = 0 via
    // .reload config takes effect immediately for pending rows.
    if (channel == "general"
        && !sLLMChatterConfig->_generalChannelEnable)
    {
        FinalizeDroppedMessage(
            messageId, eventId, sequence,
            eventType, "general_disabled");
        return DeliveryOutcome::Handled;
    }

    // Master GroupChatter toggle. Party/raid channels are
    // shared by group, raid-boss, and BG chatter, so we gate
    // on owner_subsystem (the authoritative classifier set at
    // insert time) rather than channel. Group-owned rows are
    // consumed when group chatter is disabled; raid/bg rows
    // (tagged 'raid'/'bg') are untouched. Takes effect
    // immediately for pending rows via .reload config.
    if (ownerSubsystem == "group"
        && !sLLMChatterConfig->_useGroupChatter)
    {
        FinalizeDroppedMessage(
            messageId, eventId, sequence,
            eventType, "group_chatter_disabled");
        return DeliveryOutcome::Handled;
    }

    // Master ProximityChatter toggle. Consume already-queued
    // proximity rows (open-world say/msay) when proximity
    // chatter is disabled, so flipping
    // LLMChatter.ProximityChatter.Enable = 0 via .reload
    // config takes effect immediately for pending rows too.
    if ((ownerSubsystem == "proximity"
            || ownerSubsystem == "boss_dialogue")
        && !sLLMChatterConfig->_proxChatterEnable)
    {
        FinalizeDroppedMessage(
            messageId, eventId, sequence,
            eventType, "proximity_chatter_disabled");
        return DeliveryOutcome::Handled;
    }
    if (ownerSubsystem == "boss_dialogue"
        && !sLLMChatterConfig->_proxBossDialogueEnable)
    {
        FinalizeDroppedMessage(
            messageId, eventId, sequence,
            eventType, "boss_dialogue_disabled");
        return DeliveryOutcome::Handled;
    }

    // Master GuildChatter toggle. Consume already-queued
    // guild rows when guild chatter is disabled, so flipping
    // LLMChatter.GuildChatter.Enable = 0 takes effect
    // immediately for pending rows (otherwise the generic
    // retry path resets delivered = 0 and retries forever).
    if (ownerSubsystem == "guild"
        && !sLLMChatterConfig->_guildChatterEnable)
    {
        FinalizeDroppedMessage(
            messageId, eventId, sequence,
            eventType, "guild_chatter_disabled");
        return DeliveryOutcome::Handled;
    }

    ObjectGuid guid =
        ObjectGuid::Create<HighGuid::Player>(
            botGuid);
    Player* bot =
        ObjectAccessor::FindPlayer(guid);

    if (bot)
    {
        WorldSession* session =
            bot->GetSession();
        if (session && session->PlayerLoading())
            bot = nullptr;
    }

    if (bot && eventSubjectGuid
        && IsFactionBoundReplyEvent(eventType))
    {
        Player* subject = ObjectAccessor::FindPlayer(
            ObjectGuid::Create<HighGuid::Player>(
                eventSubjectGuid));
        if (subject
            && subject->GetTeamId()
                != bot->GetTeamId())
        {
            FinalizeDroppedMessage(
                messageId, eventId, sequence,
                eventType, "faction_mismatch");
            return DeliveryOutcome::Handled;
        }
    }

    if (reactiveParty || reactiveGeneral || reactiveWhisper)
    {
        Player* source = ObjectAccessor::FindPlayer(ObjectGuid::Create<HighGuid::Player>(eventSubjectGuid));
        if (!bot || !IsPlayerBot(bot) || !bot->IsInWorld() || !source || IsPlayerBot(source) ||
            !source->IsInWorld() || bot->GetTeamId() != source->GetTeamId())
        {
            dropReasonOut = "reactive_participant_unavailable";
            return DeliveryOutcome::Dropped;
        }

        std::string const processedMessage = ConvertAllLinks(message);
        if (processedMessage.empty())
        {
            dropReasonOut = "empty_reactive_reply";
            return DeliveryOutcome::Dropped;
        }

        // A reply generated from a live observation ("mana sec", "I can see
        // the rare") is dropped if that observation stopped being true.
        if (std::string claimReason; !ReactiveClaimsHold(row.claimJson, bot, claimReason))
        {
            dropReasonOut = claimReason;
            return DeliveryOutcome::Dropped;
        }

        if (reactiveWhisper)
        {
            PlayerSocial* social = source->GetSocial();
            if (row.reactiveAudience != WhisperReactiveAudience(source, bot) ||
                (social && social->HasIgnore(bot->GetGUID())))
            {
                dropReasonOut = "stale_whisper_audience";
                return DeliveryOutcome::Dropped;
            }
            if (!withinDeadline())
                return DeliveryOutcome::Expired;
            bot->Whisper(processedMessage, LANG_UNIVERSAL, source);
        }
        else if (reactiveParty)
        {
            Group* group = source->GetOriginalGroup();
            if (!group)
                group = source->GetGroup();
            Group* botGroup = bot->GetOriginalGroup();
            if (!botGroup)
                botGroup = bot->GetGroup();
            if (!group || botGroup != group || group->GetGUID().GetCounter() != groupId ||
                !LLMChatterDeliveryPolicy::MatchesReactiveAudience(row.reactiveAudience,
                    PartyReactiveAudience(group, source->GetGUID()), PartyReactiveAudience(group, bot->GetGUID())))
            {
                dropReasonOut = "stale_party_audience";
                return DeliveryOutcome::Dropped;
            }
            if (!withinDeadline())
                return DeliveryOutcome::Expired;
            SendPartyMessageInstant(bot, group, processedMessage, "");
            RecordPartyChatGateActivity(groupId, deliveryPolicy, deliveryReason);
        }
        else
        {
            Channel* destination = nullptr;
            if (ChannelMgr* manager = ChannelMgr::forTeam(bot->GetTeamId()))
            {
                for (auto const& [name, candidate] : manager->GetChannels())
                {
                    if (candidate && candidate->GetChannelId() == ChatChannelId::GENERAL &&
                        source->IsInChannel(candidate) &&
                        LLMChatterDeliveryPolicy::MatchesReactiveAudience(row.reactiveAudience,
                            GeneralReactiveAudience(source, candidate->GetName()),
                            GeneralReactiveAudience(bot, candidate->GetName())))
                    {
                        destination = candidate;
                        break;
                    }
                }
            }
            if (!destination)
            {
                dropReasonOut = "stale_general_audience";
                return DeliveryOutcome::Dropped;
            }
            if (!withinDeadline())
                return DeliveryOutcome::Expired;
            if (!bot->IsInChannel(destination))
                destination->JoinChannel(bot, "");
            if (!destination->CanSpeak(bot->GetGUID()))
            {
                dropReasonOut = "reactive_speaker_cannot_use_channel";
                return DeliveryOutcome::Dropped;
            }
            if (!withinDeadline())
                return DeliveryOutcome::Expired;
            destination->Say(bot->GetGUID(), processedMessage, LANG_UNIVERSAL);
        }
        StoreReactiveChat(row.reactiveAudience, bot, true, message);
        return DeliveryOutcome::Sent;
    }

    if ((deliveryReason == "group_idle" || deliveryReason == "group_idle_conv") && bot)
    {
        Group* group = bot->GetGroup();
        if (!group || group->GetGUID().GetCounter() != groupId ||
            HasRecentPartyReactiveTurn(groupId, sLLMChatterConfig->_reactivePartyExpirySeconds))
        {
            dropReasonOut = "party_idle_audience_busy";
            return DeliveryOutcome::Dropped;
        }
        for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
        {
            if (Player* member = ref->GetSource(); member && member->IsInCombat())
            {
                dropReasonOut = "party_idle_in_combat";
                return DeliveryOutcome::Dropped;
            }
        }
    }

    // Only mark delivered after a successful
    // send (or if the bot is unavailable and
    // retrying would not help).
    bool sent = false;
    bool botUnavailable =
        (channel == "msay" || channel == "myell")
            ? false
            : !bot || !bot->IsInWorld();
    std::string dropReason = botUnavailable
        ? "speaker_unavailable" : "";

    ObjectGuid playerObjGuid =
        ObjectGuid::Create<HighGuid::Player>(
            playerGuid);
    Player* anchorPlayer =
        ObjectAccessor::FindPlayer(playerObjGuid);

    bool proximityLocal =
        ownerSubsystem == "proximity"
        && (channel == "say" || channel == "msay");
    bool addressedPlayerSay =
        (eventType == "proximity_player_say"
            || eventType
                == "proximity_player_conversation")
        && HasNonEmptyJsonString(
            eventExtraData, "addressed_name");
    bool allowMountedProximityBot =
        addressedPlayerSay
        || eventType == "proximity_player_emote"
        || eventType == "proximity_reply";
    float proximityRadius = static_cast<float>(
        std::max(
            sLLMChatterConfig->_proxChatterScanRadius,
            sLLMChatterConfig
                ->_proxChatterPlayerSayScanRadius));
    if (proximityLocal)
    {
        bool anchorValid =
            IsProximityAnchorEligible(anchorPlayer)
            && (!hasEventMapId
                || anchorPlayer->GetMapId()
                    == eventMapId)
            && (!eventInstanceId
                || (anchorPlayer->GetMap()
                    && anchorPlayer->GetMap()
                           ->GetInstanceId()
                        == eventInstanceId));
        if (!anchorValid)
        {
            bot = nullptr;
            anchorPlayer = nullptr;
            botUnavailable = true;
            dropReason = "anchor_player_invalid";
        }
        else if (channel == "say"
            && !IsProximityPlayerbotEligible(
                anchorPlayer, bot, proximityRadius,
                allowMountedProximityBot))
        {
            bot = nullptr;
            botUnavailable = true;
            dropReason = "speaker_ineligible";
        }
    }

    Unit* explicitAddressee = nullptr;
    if (anchorPlayer && anchorPlayer->IsInWorld())
    {
        if (addresseePlayerGuid
            == anchorPlayer->GetGUID().GetCounter())
        {
            explicitAddressee = anchorPlayer;
        }
        else if (addresseeNPCSpawnId)
        {
            explicitAddressee = FindCreatureBySpawnId(
                anchorPlayer->GetMap(),
                addresseeNPCSpawnId);
        }
        else if (addresseeBotGuid)
        {
            ObjectGuid addresseeGuid =
                ObjectGuid::Create<HighGuid::Player>(
                    addresseeBotGuid);
            Player* addresseeBot =
                ObjectAccessor::FindPlayer(addresseeGuid);
            if (addresseeBot && addresseeBot->IsInWorld()
                && addresseeBot->GetMap()
                    == anchorPlayer->GetMap())
            {
                explicitAddressee = addresseeBot;
            }
        }
    }

    if (bot && bot->IsInWorld())
    {
        if (PlayerbotAI* ai =
                GET_PLAYERBOT_AI(bot))
        {
            Player* emoteTarget = nullptr;
            if (channel == "party" || channel == "raid")
            {
                Group* grp = bot->GetGroup();
                if (grp)
                {
                    emoteTarget = FindMentionedMember(
                        bot, grp, message);
                    if (emoteTarget
                        && !emoteTarget->IsInWorld())
                        emoteTarget = nullptr;
                }
            }

            if (sLLMChatterConfig->_facingEnable
                && !bot->IsInCombat()
                && IsSafeForChatterFacing(bot))
            {
                bool faced = false;

                if (explicitAddressee
                    && explicitAddressee != bot
                    && explicitAddressee->IsAlive())
                {
                    bot->SetFacingToObject(
                        explicitAddressee);
                    faced = true;
                }

                if (!faced && eventId > 0)
                {
                    // target_entry/target_guid arrive with the
                    // batch fetch instead of a per-row query.
                    {
                        uint32 tEntry = row.targetEntry;
                        uint32 tGuid = row.targetGuid;
                        if (tEntry > 0)
                        {
                            float range =
                                static_cast<float>(
                                    sLLMChatterConfig
                                        ->_nearbyObjectScanRadius);
                            // tGuid encodes creature vs GO:
                            // non-zero = creature entry
                            // (not an instance GUID).
                            WorldObject* target =
                                (tGuid > 0)
                                ? static_cast<
                                    WorldObject*>(
                                    bot->FindNearestCreature(
                                        tEntry,
                                        range))
                                : static_cast<
                                    WorldObject*>(
                                    bot->FindNearestGameObject(
                                        tEntry,
                                        range));
                            if (target)
                            {
                                bot->SetFacingToObject(
                                    target);
                                faced = true;
                            }
                        }
                    }
                }

                if (!faced
                    && channel == "say"
                    && anchorPlayer
                    && anchorPlayer->IsInWorld())
                {
                    if (sequence > 0 && eventId > 0)
                    {
                        // Previous speaker of this conversation,
                        // prefetched by the batch query.
                        {
                            uint32 prevBotGuid = row.prevBotGuid;
                            uint32 prevNpcSpawnId =
                                row.prevNpcSpawnId;
                            if (prevNpcSpawnId)
                            {
                                Creature* prevCreature =
                                    FindCreatureBySpawnId(
                                        anchorPlayer->GetMap(),
                                        prevNpcSpawnId);
                                if (prevCreature)
                                {
                                    bot->SetFacingToObject(
                                        prevCreature);
                                    faced = true;
                                }
                            }
                            else if (prevBotGuid)
                            {
                                ObjectGuid prevGuid =
                                    ObjectGuid::Create
                                        <HighGuid::Player>(
                                            prevBotGuid);
                                Player* prevBot =
                                    ObjectAccessor
                                        ::FindPlayer(
                                            prevGuid);
                                if (prevBot
                                    && prevBot->IsInWorld())
                                {
                                    bot->SetFacingToObject(
                                        prevBot);
                                    faced = true;
                                }
                            }
                        }
                    }

                    if (!faced)
                    {
                        bot->SetFacingToObject(
                            anchorPlayer);
                        faced = true;
                    }
                }

                if (!faced
                    && (channel == "party"
                        || channel == "raid"))
                {
                    if (emoteTarget)
                    {
                        bot->SetFacingToObject(
                            emoteTarget);
                        faced = true;
                    }
                }

                if (!faced
                    && (channel == "party"
                        || channel == "raid"))
                {
                    Group* grp = bot->GetGroup();
                    if (grp)
                    {
                        Player* nearest = nullptr;
                        float bestDist = 1e9f;
                        for (auto const& ref :
                            grp->GetMemberSlots())
                        {
                            if (ref.guid
                                == bot->GetGUID())
                                continue;

                            Player* p =
                                ObjectAccessor
                                    ::FindPlayer(
                                        ref.guid);
                            if (!p
                                || !p->IsInWorld()
                                || IsPlayerBot(p)
                                || p->GetMapId()
                                    != bot->GetMapId())
                                continue;

                            float d =
                                bot->GetDistance(p);
                            if (d < bestDist)
                            {
                                bestDist = d;
                                nearest = p;
                            }
                        }

                        if (nearest)
                        {
                            bot->SetFacingToObject(
                                nearest);
                            faced = true;
                        }
                    }
                }
            }

            std::string processedMessage =
                ConvertAllLinks(message);

            bool emoteOnly = processedMessage.empty()
                && !emoteName.empty()
                && (channel == "say"
                    || (channel == "party"
                        && bot->GetGroup()));
            if (emoteOnly)
            {
                bool bgEmoteBlocked =
                    (channel == "battleground"
                        || (channel == "party"
                            && bot->GetBattleground()))
                    && !IsBGAllowedEmote(emoteName);
                if (bgEmoteBlocked)
                {
                    botUnavailable = true;
                    dropReason = "bg_emote_blocked";
                }
                else
                {
                    uint32 textEmoteId =
                        GetTextEmoteId(emoteName);
                    if (textEmoteId)
                    {
                        std::string emoteTargetName =
                            explicitAddressee
                                ? explicitAddressee->GetName()
                                : (emoteTarget
                                    ? emoteTarget->GetName()
                                    : "");
                        if (emoteName == "talk"
                            && emoteTargetName.empty())
                        {
                            if (!withinDeadline())
                                return DeliveryOutcome::Expired;
                            PlayUnitTextEmoteAnimation(
                                bot, textEmoteId);
                        }
                        else
                        {
                            if (!withinDeadline())
                                return DeliveryOutcome::Expired;
                            SendBotTextEmote(
                                bot, textEmoteId,
                                emoteTargetName);
                        }
                        sent = true;
                    }
                    else
                    {
                        botUnavailable = true;
                        dropReason = "invalid_emote";
                    }
                }
            }
            else if (channel == "party")
            {
                Group* grp = bot->GetGroup();
                if (grp && grp->isRaidGroup())
                {
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    SendPartyMessageInstant(
                        bot, grp, processedMessage,
                        "");
                    sent = true;
                }
                else
                {
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    sent = ai->SayToParty(
                        processedMessage);
                }
            }
            else if (channel == "raid")
            {
                Group* grp = bot->GetGroup();
                if (grp)
                {
                    WorldPacket data;
                    ChatHandler::BuildChatPacket(
                        data,
                        CHAT_MSG_RAID,
                        bot->GetTeamId()
                                == TEAM_ALLIANCE
                            ? LANG_COMMON
                            : LANG_ORCISH,
                        bot, nullptr,
                        processedMessage);
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    grp->BroadcastPacket(
                        &data, false);
                    sent = true;
                }
            }
            else if (channel == "battleground")
            {
                Group* grp = bot->GetGroup();
                if (grp)
                {
                    WorldPacket data;
                    ChatHandler::BuildChatPacket(
                        data,
                        CHAT_MSG_BATTLEGROUND,
                        bot->GetTeamId()
                                == TEAM_ALLIANCE
                            ? LANG_COMMON
                            : LANG_ORCISH,
                        bot, nullptr,
                        processedMessage);
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    grp->BroadcastPacket(
                        &data, false);
                    sent = true;
                }
            }
            else if (channel == "say")
            {
                if (!withinDeadline())
                    return DeliveryOutcome::Expired;
                sent = ai->Say(processedMessage);
            }
            else if (channel == "guild")
            {
                // The master-toggle guard above already
                // consumes guild rows when the feature is
                // off, so we only reach here when enabled.
                Guild* guild = bot->GetGuild();
                WorldSession* session =
                    bot->GetSession();

                if (guild && session)
                {
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    guild->BroadcastToGuild(
                        session, false,
                        processedMessage.c_str(),
                        LANG_UNIVERSAL);
                    sent = true;
                }
                else
                {
                    // a missing guild/session is not
                    // transient -> do not retry forever.
                    botUnavailable = true;
                }
            }
            else if (channel == "yell")
            {
                if (!bot->IsAlive())
                {
                }
                else if (eventZoneId
                    && bot->GetZoneId()
                        != eventZoneId)
                {
                }
                else
                {
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    sent = ai->Yell(
                        processedMessage);
                }
            }
            else if (channel == "trade")
            {
                Channel* ch =
                    EnsureBotInChatChannel(
                        bot,
                        ChatChannelId::TRADE);

                if (ch)
                {
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    ch->Say(
                        bot->GetGUID(),
                        processedMessage.c_str(),
                        LANG_UNIVERSAL);
                    sent = true;
                }
            }
            else if (channel == "lookingforgroup")
            {
                EnsureBotInChatChannel(
                    bot,
                    ChatChannelId::LOOKING_FOR_GROUP);

                if (!withinDeadline())
                    return DeliveryOutcome::Expired;
                sent = ai->SayToChannel(
                    processedMessage,
                    ChatChannelId::LOOKING_FOR_GROUP);
            }
            else if (channel == "guild_recruitment")
            {
                Channel* ch =
                    EnsureBotInChatChannel(
                        bot,
                        ChatChannelId::GUILD_RECRUITMENT);

                if (ch)
                {
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    ch->Say(
                        bot->GetGUID(),
                        processedMessage.c_str(),
                        LANG_UNIVERSAL);
                    sent = true;
                }
            }
            else
            {
                // Force-enroll the bot in the
                // correct General channel before
                // sending. Bots selected by Python
                // may never have been enrolled if
                // they spawned in the zone without
                // a zone change.
                EnsureBotInGeneralChannel(bot);

                ChannelMgr* cMgr =
                    ChannelMgr::forTeam(
                        bot->GetTeamId());
                if (cMgr)
                {
                    uint32 zId = bot->GetZoneId();
                    AreaTableEntry const* ar =
                        sAreaTableStore
                            .LookupEntry(zId);
                    if (ar)
                    {
                        uint8 loc = sWorld
                            ->GetDefaultDbcLocale();
                        char const* zn =
                            ar->area_name[loc];
                        std::string zName =
                            zn ? zn : "";
                        if (zName.empty())
                        {
                            zn = ar->area_name[
                                LOCALE_enUS];
                            zName = zn ? zn : "";
                        }

                        ChatChannelsEntry const*
                            chEntry =
                                sChatChannelsStore
                                    .LookupEntry(
                                        ChatChannelId
                                            ::GENERAL);
                        if (chEntry && !zName.empty())
                        {
                            char buf[100];
                            std::snprintf(
                                buf, sizeof(buf),
                                chEntry
                                    ->pattern[loc],
                                zName.c_str());

                            std::string exactName(buf);
                            for (auto const& [k, ch] :
                                cMgr->GetChannels())
                            {
                                if (!ch)
                                    continue;
                                if (ch->GetName()
                                    != exactName)
                                    continue;
                                if (!bot->IsInChannel(
                                        ch))
                                    continue;

                                if (!withinDeadline())
                                    return DeliveryOutcome::Expired;
                                ch->Say(
                                    bot->GetGUID(),
                                    processedMessage
                                        .c_str(),
                                    LANG_UNIVERSAL);
                                sent = true;
                                break;
                            }
                        }
                    }
                }
            }

            if (sent
                && !emoteOnly
                && !emoteName.empty()
                && channel != "general"
                && channel != "yell")
            {
                if (!((channel == "battleground"
                        || (channel == "party"
                            && bot->GetBattleground()))
                        && !IsBGAllowedEmote(
                            emoteName)))
                {
                    uint32 textEmoteId =
                        GetTextEmoteId(emoteName);
                    if (textEmoteId)
                    {
                        std::string emoteTargetName =
                            explicitAddressee
                                ? explicitAddressee->GetName()
                                : (emoteTarget
                                    ? emoteTarget->GetName()
                                    : "");
                        if (emoteName == "talk"
                            && emoteTargetName.empty())
                        {
                            PlayUnitTextEmoteAnimation(
                                bot, textEmoteId);
                        }
                        else
                        {
                            SendBotTextEmote(
                                bot, textEmoteId,
                                emoteTargetName);
                        }
                    }
                }
            }
        }
    }
    else if (channel == "msay"
        && anchorPlayer
        && anchorPlayer->IsInWorld())
    {
        Creature* speaker = FindCreatureBySpawnId(
            anchorPlayer->GetMap(), npcSpawnId);
        bool speakerUnavailable =
            !IsProximityNPCEligible(
                anchorPlayer, speaker,
                proximityRadius);
        if (speakerUnavailable)
        {
            botUnavailable = true;
            dropReason = "npc_speaker_unavailable";
        }
        else
        {
            float originalOrientation =
                speaker->GetOrientation();
            SceneFacingKey facingKey = {
                eventId, npcSpawnId,
            };
            bool facingApplied = false;
            if (sLLMChatterConfig->_facingEnable
                && IsSafeForChatterFacing(speaker))
            {
                if (eventId
                    && sLLMChatterConfig
                        ->_proxChatterFacingResetDelay > 0)
                {
                    auto inserted =
                        _sceneOriginalFacings.emplace(
                            facingKey,
                            SceneFacingState{
                                originalOrientation,
                                now,
                                playerGuid});
                    originalOrientation = inserted.first
                        ->second.orientation;
                    inserted.first->second.updatedAt = now;
                }
                bool faced = false;
                if (explicitAddressee
                    && explicitAddressee != speaker
                    && explicitAddressee->IsAlive())
                {
                    speaker->SetFacingToObject(
                        explicitAddressee);
                    faced = true;
                    facingApplied = true;
                }
                if (!faced && sequence > 0 && eventId > 0)
                {
                    // Previous speaker, prefetched by the batch query.
                    {
                        uint32 prevBotGuid = row.prevBotGuid;
                        uint32 prevNpcSpawnId = row.prevNpcSpawnId;
                        if (prevNpcSpawnId)
                        {
                            Creature* prevCreature =
                                FindCreatureBySpawnId(
                                    anchorPlayer->GetMap(),
                                    prevNpcSpawnId);
                            if (prevCreature)
                            {
                                speaker->SetFacingToObject(
                                    prevCreature);
                                faced = true;
                                facingApplied = true;
                            }
                        }
                        else if (prevBotGuid)
                        {
                            ObjectGuid prevGuid =
                                ObjectGuid::Create
                                    <HighGuid::Player>(
                                        prevBotGuid);
                            Player* prevBot =
                                ObjectAccessor
                                    ::FindPlayer(
                                        prevGuid);
                            if (prevBot
                                && prevBot->IsInWorld())
                            {
                                speaker->SetFacingToObject(
                                    prevBot);
                                faced = true;
                                facingApplied = true;
                            }
                        }
                    }
                }

                if (!faced)
                {
                    speaker->SetFacingToObject(
                        anchorPlayer);
                    facingApplied = true;
                }
            }
            std::string msayMessage =
                ConvertAllLinks(message);
            bool msayEmoteOnly =
                msayMessage.empty()
                && !emoteName.empty();
            if (!msayMessage.empty())
            {
                if (!withinDeadline())
                    return DeliveryOutcome::Expired;
                speaker->Say(
                    msayMessage, LANG_UNIVERSAL);
                sent = true;
            }

            if (!emoteName.empty())
            {
                uint32 textEmoteId =
                    GetTextEmoteId(emoteName);
                if (textEmoteId)
                {
                    std::string targetName =
                        explicitAddressee
                            ? explicitAddressee->GetName()
                            : anchorPlayer->GetName();
                    if (!withinDeadline())
                        return DeliveryOutcome::Expired;
                    SendUnitTextEmote(
                        speaker, textEmoteId,
                        targetName);
                    sent = true;
                }
                else if (msayEmoteOnly)
                {
                    botUnavailable = true;
                    dropReason = "invalid_emote";
                }
            }

            if (sLLMChatterConfig
                    ->_proxChatterFacingResetDelay > 0)
            {
                bool hasLaterLine =
                    eventId && row.hasLaterUndelivered;
                if (!hasLaterLine && eventId)
                {
                    ScheduleSceneFacingResets(
                        eventId,
                        sLLMChatterConfig
                            ->_proxChatterFacingResetDelay);
                }
                else if (!hasLaterLine && facingApplied)
                {
                    speaker->m_Events.AddEvent(
                        new DelayedNPCFacingResetEvent(
                            playerObjGuid,
                            npcSpawnId,
                            originalOrientation),
                        speaker->m_Events.CalculateTime(
                            sLLMChatterConfig
                                    ->_proxChatterFacingResetDelay
                                * IN_MILLISECONDS));
                }
            }
        }
    }

    else if (channel == "myell"
        && ownerSubsystem == "boss_dialogue"
        && anchorPlayer
        && anchorPlayer->IsInWorld())
    {
        bool anchorValid =
            IsProximityAnchorEligible(anchorPlayer)
            && (!hasEventMapId
                || anchorPlayer->GetMapId() == eventMapId)
            && (!eventInstanceId
                || (anchorPlayer->GetMap()
                    && anchorPlayer->GetMap()->GetInstanceId()
                        == eventInstanceId));
        Creature* speaker = anchorValid
            ? FindCreatureBySpawnId(
                anchorPlayer->GetMap(), npcSpawnId)
            : nullptr;
        bool speakerUnavailable =
            !anchorValid
            || !IsBossDialogueSpeakerEligible(
                anchorPlayer,
                speaker,
                static_cast<float>(
                    sLLMChatterConfig
                        ->_proxBossApproachMaxRadius));
        if (speakerUnavailable)
        {
            botUnavailable = true;
            dropReason = "boss_speaker_unavailable";
        }
        else
        {
            if (!withinDeadline())
                return DeliveryOutcome::Expired;
            speaker->Yell(
                ConvertAllLinks(message),
                LANG_UNIVERSAL);
            sent = true;
        }
    }

    if (sent && eventId > 0
        && playerGuid > 0
        && (channel == "say" || channel == "msay"))
    {
        bool replyEligible = !row.hasLaterUndelivered;

        std::string deliveredContext = message;
        if (deliveredContext.empty()
            && !emoteName.empty())
        {
            deliveredContext = std::string(
                "[performed /") + emoteName + "]";
        }
        RecordDeliveredProximityLine(
            eventId,
            playerGuid,
            eventZoneId,
            eventMapId,
            eventInstanceId,
            channel == "say" ? botGuid : 0,
            channel == "msay" ? npcSpawnId : 0,
            replyEligible,
            botName,
            deliveredContext);
    }

    if (sent && channel == "party")
    {
        uint32 gateGroupId = groupId;
        if (gateGroupId == 0 && bot)
        {
            if (Group* grp = bot->GetGroup())
                gateGroupId = grp->GetGUID().GetCounter();
        }

        RecordPartyChatGateActivity(
            gateGroupId,
            deliveryPolicy.empty()
                ? "contextual" : deliveryPolicy,
            deliveryReason.empty()
                ? "party_delivery" : deliveryReason);
    }

    if (sent && channel == "guild" && bot)
    {
        RecordDeliveredGuildLine(
            bot->GetGuildId(),
            eventId,
            botGuid,
            botName,
            message,
            &eventType);
    }

    if (sent)
        return DeliveryOutcome::Sent;

    if (botUnavailable)
    {
        dropReasonOut = dropReason.empty()
            ? "speaker_unavailable"
            : dropReason;
        return DeliveryOutcome::Dropped;
    }

    return DeliveryOutcome::Retry;
}

namespace
{
// Legacy behaviour expired rows 60 seconds past deliver_at; the
// fetch excludes them directly so the expiry sweep can run less often
// and asynchronously without ever delivering an expired row.
constexpr uint32 DELIVERY_EXPIRY_SECONDS = 60;

std::string BuildDeliveryFetchSql(bool priorityOrder, uint32 limit)
{
    return LLMChatterDeliveryPolicy::BuildFetchSql(
        priorityOrder, limit, DELIVERY_EXPIRY_SECONDS);
}

std::vector<DeliveryRow> ParseDeliveryRows(QueryResult result)
{
    std::vector<DeliveryRow> rows;
    if (!result)
        return rows;

    rows.reserve(result->GetRowCount());
    do
    {
        Field* fields = result->Fetch();
        DeliveryRow row;
        row.messageId = fields[0].Get<uint32>();
        row.botGuid = FieldOr<uint32>(fields[1], 0);
        row.botName = FieldOr<std::string>(fields[2], "");
        row.message = FieldOr<std::string>(fields[3], "");
        row.channel = FieldOr<std::string>(fields[4], "");
        row.emoteName = FieldOr<std::string>(fields[5], "");
        row.npcSpawnId = FieldOr<uint32>(fields[6], 0);
        row.playerGuid = FieldOr<uint32>(fields[7], 0);
        row.sequence = FieldOr<uint32>(fields[8], 0);
        row.eventId = FieldOr<uint32>(fields[9], 0);
        row.eventZoneId = FieldOr<uint32>(fields[10], 0);
        row.groupId = FieldOr<uint32>(fields[11], 0);
        row.deliveryPolicy = FieldOr<std::string>(fields[12], "");
        row.deliveryReason = FieldOr<std::string>(fields[13], "");
        row.ownerSubsystem = FieldOr<std::string>(fields[14], "");
        row.addresseePlayerGuid = FieldOr<uint32>(fields[15], 0);
        row.addresseeBotGuid = FieldOr<uint32>(fields[16], 0);
        row.addresseeNPCSpawnId = FieldOr<uint32>(fields[17], 0);
        row.hasEventMapId = !fields[18].IsNull();
        row.eventMapId = FieldOr<uint32>(fields[18], 0);
        row.eventExtraData = FieldOr<std::string>(fields[19], "");
        row.eventType = FieldOr<std::string>(fields[20], "");
        row.eventSubjectGuid = FieldOr<uint32>(fields[21], 0);
        row.queueId = FieldOr<uint32>(fields[22], 0);
        row.targetEntry = FieldOr<uint32>(fields[23], 0);
        row.targetGuid = FieldOr<uint32>(fields[24], 0);
        row.prevBotGuid = FieldOr<uint32>(fields[25], 0);
        row.prevNpcSpawnId = FieldOr<uint32>(fields[26], 0);
        row.hasLaterUndelivered =
            FieldOr<uint32>(fields[27], 0) != 0;
        row.remainingUs = FieldOr<int64>(fields[28], -1);
        row.reactiveAudience = FieldOr<std::string>(fields[29], "");
        row.reactiveTurn = FieldOr<std::string>(fields[30], "");
        row.claimJson = FieldOr<std::string>(fields[31], "");
        rows.push_back(std::move(row));
    } while (result->NextRow());

    return rows;
}

// Chat destination used to pace delivery: at most one line per
// destination per tick, as the legacy one-row loop guaranteed.
std::string DeliveryDestinationKey(DeliveryRow const& row)
{
    std::string const& channel = row.channel;
    if (channel == "say" || channel == "msay"
        || channel == "myell")
    {
        // Local scenes are anchored to one real player.
        return "local:" + std::to_string(row.playerGuid);
    }
    if (channel == "whisper")
        return "whisper:" + std::to_string(row.eventSubjectGuid) + ":" + std::to_string(row.botGuid);

    Player* bot = row.botGuid
        ? ObjectAccessor::FindPlayer(
            ObjectGuid::Create<HighGuid::Player>(row.botGuid))
        : nullptr;

    if (channel == "party" || channel == "raid"
        || channel == "battleground")
    {
        if (row.groupId)
            return "group:" + std::to_string(row.groupId);
        if (bot && bot->GetGroup())
            return "group:" + std::to_string(
                bot->GetGroup()->GetGUID().GetCounter());
    }
    else if (channel == "guild")
    {
        if (bot && bot->GetGuildId())
            return "guild:" + std::to_string(bot->GetGuildId());
    }
    else if (bot)
    {
        // General, trade, LFG, recruitment and yell are zone/team
        // scoped broadcasts.
        return channel + ":" + std::to_string(bot->GetTeamId())
            + ":" + std::to_string(bot->GetZoneId());
    }

    // Unknown speaker: keep it from blocking anything else; delivery
    // will drop it as unavailable.
    return "msg:" + std::to_string(row.messageId);
}

std::string JoinIds(std::vector<uint32> const& ids)
{
    std::string joined;
    for (uint32 id : ids)
    {
        if (!joined.empty())
            joined += ',';
        joined += std::to_string(id);
    }
    return joined;
}

void ProcessFetchedRows(QueryResult result, std::chrono::steady_clock::time_point issuedAt)
{
    auto const startedAt = std::chrono::steady_clock::now();
    time_t const now = time(nullptr);
    _handledRows.Prune(now);
    _cancelledConversations.Prune(now);

    std::vector<DeliveryRow> rows =
        ParseDeliveryRows(std::move(result));
    _diag.rowsFetched += rows.size();
    if (rows.empty())
        return;

    std::vector<LLMChatterDeliveryBatch::Candidate> candidates;
    candidates.reserve(rows.size());
    for (DeliveryRow const& row : rows)
    {
        LLMChatterDeliveryBatch::Candidate candidate;
        candidate.messageId = row.messageId;
        if (row.eventId)
            candidate.conversationKey =
                "e" + std::to_string(row.eventId);
        else if (row.queueId)
            candidate.conversationKey =
                "q" + std::to_string(row.queueId);
        candidate.destinationKey = DeliveryDestinationKey(row);
        candidate.channel = row.channel;
        candidates.push_back(std::move(candidate));
    }

    std::unordered_set<uint32> guarded;
    for (auto const& candidate : candidates)
    {
        if (_handledRows.Contains(candidate.messageId, now))
            guarded.insert(candidate.messageId);
    }

    LLMChatterDeliveryBatch::Limits limits;
    limits.maxBatch = sLLMChatterConfig->_deliveryBatchSize;
    limits.maxPerChannel = sLLMChatterConfig->_deliveryMaxPerChannel;
    LLMChatterDeliveryBatch::Selection selection =
        LLMChatterDeliveryBatch::SelectBatch(
            candidates, limits, guarded);

    for (LLMChatterDeliveryBatch::SkipReason reason
        : selection.reasons)
    {
        if (reason == LLMChatterDeliveryBatch::SkipReason
                ::RecentlyHandled)
            ++_diag.guardSkipped;
        else if (reason != LLMChatterDeliveryBatch::SkipReason::None)
            ++_diag.deferred;
    }

    std::vector<uint32> sentIds;
    for (size_t index : selection.selected)
    {
        DeliveryRow const& row = rows[index];

        if (_cancelledConversations.IsCancelled(
                row.eventId, row.sequence, now))
        {
            // The DB cancellation is queued; record nothing more.
            _handledRows.Mark(
                row.messageId, now, HANDLED_ROW_HOLD_SECONDS);
            ++_diag.guardSkipped;
            continue;
        }

        // Recheck each row immediately before dispatch, including time spent
        // processing earlier members of this batch. Round elapsed time up so
        // sub-microsecond truncation cannot extend the database deadline.
        int64 const elapsedUs = std::chrono::ceil<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - issuedAt).count();
        auto const decision = LLMChatterDeliveryPolicy::CheckDispatch(
            row.remainingUs, elapsedUs,
            !IsLLMChatterAsyncShutdown() && sLLMChatterConfig
                && sLLMChatterConfig->IsEnabled());
        if (decision == LLMChatterDeliveryPolicy::DispatchDecision::Stop)
            break;
        std::string dropReason;
        DeliveryOutcome outcome = decision == LLMChatterDeliveryPolicy::DispatchDecision::Expire
            ? DeliveryOutcome::Expired : DeliverRow(row, now, dropReason, issuedAt);

        if (sLLMChatterConfig->_deliveryVerboseLog)
        {
            LOG_INFO("module",
                "[LLMChatter] delivery id={} channel={} event={} "
                "seq={} outcome={}{}",
                row.messageId, row.channel, row.eventId,
                row.sequence, static_cast<uint32>(outcome),
                dropReason.empty() ? "" : " reason=" + dropReason);
        }

        switch (outcome)
        {
            case DeliveryOutcome::Expired:
                // Expiry terminates this line only, as the periodic sweep does.
                // No successful Phase 2 confirmation for this terminal status.
                _handledRows.Mark(row.messageId, now, HANDLED_ROW_HOLD_SECONDS);
                ++_diag.dropped["expired_before_delivery"];
                ExecuteLLMChatterWrite(Acore::StringFormat(
                    "UPDATE llm_chatter_messages SET delivered = 1, "
                    "delivered_at = NOW(), drop_reason = 'expired_before_delivery' "
                    "WHERE id = {} AND delivered = 0", row.messageId));
                break;
            case DeliveryOutcome::Sent:
                sentIds.push_back(row.messageId);
                _handledRows.Mark(
                    row.messageId, now, HANDLED_ROW_HOLD_SECONDS);
                break;
            case DeliveryOutcome::Dropped:
                FinalizeDroppedMessage(
                    row.messageId, row.eventId, row.sequence,
                    row.eventType, dropReason.c_str());
                break;
            case DeliveryOutcome::Retry:
                ++_diag.retried;
                _handledRows.Mark(
                    row.messageId, now, RETRY_ROW_HOLD_SECONDS);
                // Reschedule for retry
                ExecuteLLMChatterWrite(Acore::StringFormat(
                    "UPDATE llm_chatter_messages "
                    "SET delivered = 0, "
                    "drop_reason = NULL, "
                    "deliver_at = DATE_ADD("
                    "NOW(), INTERVAL 5 SECOND) "
                    "WHERE id = {}",
                    row.messageId));
                break;
            case DeliveryOutcome::Handled:
                _handledRows.Mark(
                    row.messageId, now, HANDLED_ROW_HOLD_SECONDS);
                break;
        }
    }

    if (!sentIds.empty())
    {
        // One write for every line spoken this tick.
        ExecuteLLMChatterWrite(Acore::StringFormat(
            "UPDATE llm_chatter_messages "
            "SET delivered = 1, "
            "delivered_at = NOW(), "
            "drop_reason = NULL "
            "WHERE id IN ({})",
            JoinIds(sentIds)));
    }

    _diag.delivered += sentIds.size();
    ++_diag.batches;
    _diag.selected += selection.selected.size();
    _diag.maxBatch = std::max<uint32>(
        _diag.maxBatch,
        static_cast<uint32>(selection.selected.size()));
    uint32 const elapsedUs = static_cast<uint32>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - startedAt).count());
    _diag.callbackTotalUs += elapsedUs;
    _diag.callbackMaxUs = std::max(_diag.callbackMaxUs, elapsedUs);
}

void SweepExpiredMessages(time_t now)
{
    if (now - _lastExpireSweep
        < static_cast<time_t>(
            sLLMChatterConfig->_deliveryExpireSweepSeconds))
        return;
    _lastExpireSweep = now;

    ExecuteLLMChatterWrite(Acore::StringFormat(
        "UPDATE llm_chatter_messages "
        "SET delivered = 1, delivered_at = NOW(), "
        "drop_reason = 'expired_before_delivery' "
        "WHERE delivered = 0 "
        "AND deliver_at < DATE_SUB(NOW(), "
        "INTERVAL {} SECOND)",
        DELIVERY_EXPIRY_SECONDS));
}

void ReportDiagnostics(time_t now)
{
    uint32 const interval =
        sLLMChatterConfig->_deliveryDiagnosticsInterval;
    if (!interval)
        return;
    if (!_lastDiagnosticsReport)
    {
        _lastDiagnosticsReport = now;
        return;
    }
    if (now - _lastDiagnosticsReport < static_cast<time_t>(interval))
        return;
    _lastDiagnosticsReport = now;

    std::string drops;
    for (auto const& [reason, total] : _diag.dropped)
    {
        if (!drops.empty())
            drops += ',';
        drops += reason + "=" + std::to_string(total);
    }

    LOG_INFO("module",
        "[LLMChatter] delivery stats {}s: fetches={} rows={} "
        "batches={} selected={} max_batch={} delivered={} "
        "retried={} deferred={} guard_skipped={} "
        "fetch_ms_avg={} fetch_ms_max={} process_us_avg={} "
        "process_us_max={} dropped=[{}]",
        interval, _diag.fetches, _diag.rowsFetched,
        _diag.batches, _diag.selected, _diag.maxBatch,
        _diag.delivered, _diag.retried, _diag.deferred,
        _diag.guardSkipped,
        _diag.fetches
            ? _diag.fetchLatencyTotalMs / _diag.fetches : 0,
        _diag.fetchLatencyMaxMs,
        _diag.batches ? _diag.callbackTotalUs / _diag.batches : 0,
        _diag.callbackMaxUs, drops);

    _diag = DeliveryDiagnostics();

    // Backlog and expiry totals come from one optional async query so
    // the per-tick path stays unchanged when diagnostics are enabled.
    if (_diagnosticsQueryInFlight
        || !sLLMChatterConfig->_deliveryAsyncEnable)
        return;
    _diagnosticsQueryInFlight = true;
    GetLLMChatterQueryProcessor().AddCallback(
        CharacterDatabase.AsyncQuery(Acore::StringFormat(
            "SELECT "
            "SUM(delivered = 0 AND deliver_at <= NOW()) AS due_rows, "
            "SUM(delivered = 0 AND deliver_at < DATE_SUB(NOW(), "
            "INTERVAL {} SECOND)) AS overdue_rows, "
            "SUM(drop_reason = 'expired_before_delivery' "
            "AND delivered_at >= DATE_SUB(NOW(), "
            "INTERVAL {} SECOND)) AS expired_rows "
            "FROM llm_chatter_messages "
            "WHERE delivered = 0 "
            "OR delivered_at >= DATE_SUB(NOW(), INTERVAL {} SECOND)",
            DELIVERY_EXPIRY_SECONDS, interval, interval))
            .WithCallback([interval](QueryResult result)
            {
                _diagnosticsQueryInFlight = false;
                if (!result || IsLLMChatterAsyncShutdown())
                    return;
                Field* fields = result->Fetch();
                LOG_INFO("module",
                    "[LLMChatter] delivery backlog: due={} "
                    "overdue={} expired_last_{}s={}",
                    FieldOr<std::string>(fields[0], "0"),
                    FieldOr<std::string>(fields[1], "0"),
                    interval,
                    FieldOr<std::string>(fields[2], "0"));
            }));
}
} // namespace

void DeliverPendingMessagesImpl()
{
    time_t now = time(nullptr);
    for (auto it = _sceneOriginalFacings.begin();
         it != _sceneOriginalFacings.end();)
    {
        if (now - it->second.updatedAt > 120)
        {
            uint32 staleEventId = it->first.first;
            ScheduleSceneFacingResets(staleEventId, 0);
            it = _sceneOriginalFacings.begin();
        }
        else
            ++it;
    }

    if (IsLLMChatterAsyncShutdown())
        return;

    ReportDiagnostics(now);
    SweepExpiredMessages(now);

    if (_fetchInFlight)
    {
        // Never stack fetches behind a slow database; report once.
        if (!_fetchStallWarned
            && GetMSTimeDiffToNow(_fetchIssuedMs) > FETCH_STALL_WARN_MS)
        {
            _fetchStallWarned = true;
            LOG_WARN("module",
                "[LLMChatter] delivery fetch still pending after {} ms",
                GetMSTimeDiffToNow(_fetchIssuedMs));
        }
        return;
    }

    bool const priorityOrder =
        sLLMChatterConfig->_prioritySystemEnable
        && sLLMChatterConfig->_priorityDeliveryOrderEnable;
    std::string sql = BuildDeliveryFetchSql(
        priorityOrder, sLLMChatterConfig->_deliveryCandidateLimit);

    ++_diag.fetches;
    _fetchIssuedMs = getMSTime();
    auto const issuedAt = std::chrono::steady_clock::now();

    if (!sLLMChatterConfig->_deliveryAsyncEnable)
    {
        // Rollback path: the same batch logic on a blocking query.
        QueryResult result = CharacterDatabase.Query(sql);
        _diag.fetchLatencyTotalMs += GetMSTimeDiffToNow(_fetchIssuedMs);
        ProcessFetchedRows(std::move(result), issuedAt);
        return;
    }

    _fetchInFlight = true;
    _fetchStallWarned = false;
    GetLLMChatterQueryProcessor().AddCallback(
        CharacterDatabase.AsyncQuery(sql).WithCallback(
            [issuedAt](QueryResult result)
            {
                // Runs on the world thread from
                // ProcessLLMChatterAsyncCallbacks(); only plain row
                // data crosses from the database thread.
                _fetchInFlight = false;
                uint32 const latency =
                    GetMSTimeDiffToNow(_fetchIssuedMs);
                _diag.fetchLatencyTotalMs += latency;
                _diag.fetchLatencyMaxMs =
                    std::max(_diag.fetchLatencyMaxMs, latency);

                if (IsLLMChatterAsyncShutdown()
                    || !sLLMChatterConfig
                    || !sLLMChatterConfig->IsEnabled())
                    return;

                ProcessFetchedRows(std::move(result), issuedAt);
            }));
}
