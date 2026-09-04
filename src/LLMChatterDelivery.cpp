/*
 * mod-llm-chatter - outbound message delivery ownership
 */

#include "LLMChatterConfig.h"
#include "Guild.h"
#include "LLMChatterDelivery.h"
#include "LLMChatterGuild.h"
#include "LLMChatterProximity.h"
#include "LLMChatterShared.h"

#include "Channel.h"
#include "ChannelMgr.h"
#include "Chat.h"
#include "Creature.h"
#include "DatabaseEnv.h"
#include "DBCStores.h"
#include "Group.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "TradeData.h"
#include "Playerbots.h"
#include "SharedDefines.h"
#include "World.h"
#include "WorldSession.h"

#include <cstdio>
#include <limits>

namespace
{
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
} // namespace

void ProcessPendingPlayerbotActionsImpl()
{
    // Service reservations are intentionally separate from
    // immediate PlayerBot gameplay requests.
    //
    // Chatter never creates a travel target here. Once the
    // requester and reserved bot are in the same party,
    // normal Playerbots party/follow/travel behavior owns
    // movement toward the group.
    CharacterDatabase.DirectExecute(
        "UPDATE llm_playerbot_actions "
        "SET status = 'expired', "
        "result_code = 'service_timeout', "
        "result_detail = CASE "
        "WHEN status = 'waiting_party' "
        "THEN 'waiting for party timed out' "
        "WHEN status = 'routing' "
        "THEN 'party routing timed out' "
        "ELSE 'service ready window timed out' END, "
        "executed_at = NOW() "
        "WHERE status IN "
        "('waiting_party', 'routing', 'ready') "
        "AND expires_at IS NOT NULL "
        "AND expires_at <= NOW()");

    QueryResult serviceResult =
        CharacterDatabase.Query(
            "SELECT id, player_guid, bot_guid "
            "FROM llm_playerbot_actions "
            "WHERE status = 'waiting_party' "
            "AND action_key = 'lockpick_trade_item' "
            "AND (expires_at IS NULL "
            "OR expires_at > NOW()) "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT 1");

    if (serviceResult)
    {
        Field* serviceFields =
            serviceResult->Fetch();

        uint64 serviceId =
            serviceFields[0].Get<uint64>();

        uint32 servicePlayerGuid =
            serviceFields[1].Get<uint32>();

        uint32 serviceBotGuid =
            serviceFields[2].Get<uint32>();

        Player* servicePlayer =
            ObjectAccessor::FindPlayer(
                ObjectGuid::Create<HighGuid::Player>(
                    servicePlayerGuid));

        Player* serviceBot =
            ObjectAccessor::FindPlayer(
                ObjectGuid::Create<HighGuid::Player>(
                    serviceBotGuid));

        if (
            servicePlayer
            && serviceBot
            && servicePlayer->IsInWorld()
            && serviceBot->IsInWorld()
            && IsPlayerBot(serviceBot))
        {
            Group* playerGroup =
                servicePlayer->GetGroup();

            Group* botGroup =
                serviceBot->GetGroup();

            if (
                playerGroup
                && botGroup
                && playerGroup == botGroup)
            {
                uint32 groupId =
                    playerGroup->GetGUID()
                        .GetCounter();

                CharacterDatabase.DirectExecute(
                    "UPDATE llm_playerbot_actions "
                    "SET status = 'routing', "
                    "group_id = {}, "
                    "result_code = 'party_joined', "
                    "result_detail = "
                    "'party confirmed; using normal "
                    "Playerbots routing', "
                    "expires_at = DATE_ADD("
                    "NOW(), INTERVAL 120 SECOND) "
                    "WHERE id = {} "
                    "AND status = 'waiting_party'",
                    groupId,
                    serviceId);
            }
        }
    }

    QueryResult routingResult =
        CharacterDatabase.Query(
            "SELECT id, player_guid, bot_guid, group_id "
            "FROM llm_playerbot_actions "
            "WHERE status = 'routing' "
            "AND action_key = 'lockpick_trade_item' "
            "AND expires_at IS NOT NULL "
            "AND expires_at > NOW() "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT 1");

    if (routingResult)
    {
        Field* routingFields =
            routingResult->Fetch();

        uint64 routingId =
            routingFields[0].Get<uint64>();

        uint32 routingPlayerGuid =
            routingFields[1].Get<uint32>();

        uint32 routingBotGuid =
            routingFields[2].Get<uint32>();

        uint32 routingGroupId =
            routingFields[3].IsNull()
                ? 0
                : routingFields[3].Get<uint32>();

        Player* routingPlayer =
            ObjectAccessor::FindPlayer(
                ObjectGuid::Create<HighGuid::Player>(
                    routingPlayerGuid));

        Player* routingBot =
            ObjectAccessor::FindPlayer(
                ObjectGuid::Create<HighGuid::Player>(
                    routingBotGuid));

        if (
            routingPlayer
            && routingBot
            && routingPlayer->IsInWorld()
            && routingBot->IsInWorld())
        {
            if (!IsPlayerBot(routingBot))
            {
                CharacterDatabase.DirectExecute(
                    "UPDATE llm_playerbot_actions "
                    "SET status = 'cancelled', "
                    "result_code = 'not_playerbot', "
                    "result_detail = "
                    "'reserved service bot is no longer "
                    "a PlayerBot', "
                    "executed_at = NOW() "
                    "WHERE id = {} "
                    "AND status = 'routing'",
                    routingId);
            }
            else
            {
                Group* playerGroup =
                    routingPlayer->GetGroup();

                Group* botGroup =
                    routingBot->GetGroup();

                bool sameReservedParty =
                    playerGroup
                    && botGroup
                    && playerGroup == botGroup
                    && routingGroupId > 0
                    && playerGroup->GetGUID()
                        .GetCounter()
                        == routingGroupId;

                if (!sameReservedParty)
                {
                    CharacterDatabase.DirectExecute(
                        "UPDATE llm_playerbot_actions "
                        "SET status = 'cancelled', "
                        "result_code = 'party_broken', "
                        "result_detail = "
                        "'service cancelled because the "
                        "reserved party changed or disbanded', "
                        "executed_at = NOW() "
                        "WHERE id = {} "
                        "AND status = 'routing'",
                        routingId);
                }
                else if (
                    routingPlayer->GetMapId()
                        == routingBot->GetMapId()
                    && routingPlayer->IsWithinDistInMap(
                        routingBot,
                        100.0f))
                {
                    CharacterDatabase.DirectExecute(
                        "UPDATE llm_playerbot_actions "
                        "SET status = 'ready', "
                        "result_code = 'service_ready', "
                        "result_detail = "
                        "'service ready; player and bot "
                        "are locally reachable', "
                        "expires_at = DATE_ADD("
                        "NOW(), INTERVAL 120 SECOND) "
                        "WHERE id = {} "
                        "AND status = 'routing'",
                        routingId);
                }
            }
        }
    }

    QueryResult readyResult =
        CharacterDatabase.Query(
            "SELECT id, player_guid, bot_guid, "
            "group_id, action_arg "
            "FROM llm_playerbot_actions "
            "WHERE status = 'ready' "
            "AND action_key = 'lockpick_trade_item' "
            "AND expires_at IS NOT NULL "
            "AND expires_at > NOW() "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT 1");

    if (readyResult)
    {
        Field* readyFields =
            readyResult->Fetch();

        uint64 readyId =
            readyFields[0].Get<uint64>();

        uint32 readyPlayerGuid =
            readyFields[1].Get<uint32>();

        uint32 readyBotGuid =
            readyFields[2].Get<uint32>();

        uint32 readyGroupId =
            readyFields[3].IsNull()
                ? 0
                : readyFields[3].Get<uint32>();

        std::string readyActionArg =
            readyFields[4].IsNull()
                ? ""
                : readyFields[4].Get<std::string>();

        uint32 reservedItemEntry = 0;

        try
        {
            size_t parsed = 0;

            unsigned long value =
                std::stoul(
                    readyActionArg,
                    &parsed,
                    10);

            if (
                parsed
                    == readyActionArg.size()
                && value > 0
                && value
                    <= std::numeric_limits<uint32>::max())
            {
                reservedItemEntry =
                    static_cast<uint32>(
                        value);
            }
        }
        catch (...)
        {
            reservedItemEntry = 0;
        }

        if (!reservedItemEntry)
        {
            CharacterDatabase.DirectExecute(
                "UPDATE llm_playerbot_actions "
                "SET status = 'failed', "
                "result_code = 'invalid_item_entry', "
                "result_detail = "
                "'reserved lockbox item entry is invalid', "
                "executed_at = NOW() "
                "WHERE id = {} "
                "AND status = 'ready'",
                readyId);
        }
        else
        {
            Player* readyPlayer =
                ObjectAccessor::FindPlayer(
                    ObjectGuid::Create<HighGuid::Player>(
                        readyPlayerGuid));

            Player* readyBot =
                ObjectAccessor::FindPlayer(
                    ObjectGuid::Create<HighGuid::Player>(
                        readyBotGuid));

            // Keep the reservation alive if either participant
            // temporarily drops offline; normal ready timeout
            // will release it.
            if (
                readyPlayer
                && readyBot
                && readyPlayer->IsInWorld()
                && readyBot->IsInWorld())
            {
                if (!IsPlayerBot(readyBot))
                {
                    CharacterDatabase.DirectExecute(
                        "UPDATE llm_playerbot_actions "
                        "SET status = 'cancelled', "
                        "result_code = 'not_playerbot', "
                        "result_detail = "
                        "'reserved service bot is no longer "
                        "a PlayerBot', "
                        "executed_at = NOW() "
                        "WHERE id = {} "
                        "AND status = 'ready'",
                        readyId);
                }
                else
                {
                    Group* playerGroup =
                        readyPlayer->GetGroup();

                    Group* botGroup =
                        readyBot->GetGroup();

                    bool sameReservedParty =
                        playerGroup
                        && botGroup
                        && playerGroup == botGroup
                        && readyGroupId > 0
                        && playerGroup->GetGUID()
                            .GetCounter()
                            == readyGroupId;

                    if (!sameReservedParty)
                    {
                        CharacterDatabase.DirectExecute(
                            "UPDATE llm_playerbot_actions "
                            "SET status = 'cancelled', "
                            "result_code = 'party_broken', "
                            "result_detail = "
                            "'service cancelled because the "
                            "reserved party changed or disbanded', "
                            "executed_at = NOW() "
                            "WHERE id = {} "
                            "AND status = 'ready'",
                            readyId);
                    }
                    else if (
                        readyPlayer->GetMapId()
                            == readyBot->GetMapId()
                        && readyPlayer->IsWithinDistInMap(
                            readyBot,
                            100.0f))
                    {
                        Player* trader =
                            readyBot->GetTrader();

                        TradeData* playerTrade =
                            readyPlayer->GetTradeData();

                        bool exactTradePair =
                            trader == readyPlayer
                            && playerTrade
                            && playerTrade->GetTrader()
                                == readyBot;

                        if (exactTradePair)
                        {
                            Item* lockbox =
                                playerTrade->GetItem(
                                    TRADE_SLOT_NONTRADED);

                            if (
                                lockbox
                                && lockbox->GetEntry()
                                    == reservedItemEntry)
                            {
                                // Do not dispatch "unlock traded item"
                                // here. Playerbots already handles the
                                // trade update packet and invokes its
                                // native TradeStatusExtendedAction,
                                // which owns Pick Lock timing.
                                //
                                // Chatter only verifies that the exact
                                // reserved trade has been reached, then
                                // waits for native Playerbots mechanics.
                                CharacterDatabase.DirectExecute(
                                    "UPDATE llm_playerbot_actions "
                                    "SET status = 'processing', "
                                    "result_code = "
                                    "'lockpick_trade_ready', "
                                    "result_detail = "
                                    "'exact reserved lockbox observed "
                                    "in Do Not Trade slot; native "
                                    "Playerbots trade handling owns "
                                    "Pick Lock execution', "
                                    "expires_at = DATE_ADD("
                                    "NOW(), INTERVAL 30 SECOND) "
                                    "WHERE id = {} "
                                    "AND status = 'ready'",
                                    readyId);
                            }
                        }
                    }
                }
            }
        }
    }

    // A lockpick service in processing has already reached
    // the exact reserved trade. Chatter does not cast Pick Lock;
    // it only observes the outcome produced by native Playerbots.
    CharacterDatabase.DirectExecute(
        "UPDATE llm_playerbot_actions "
        "SET status = 'expired', "
        "result_code = 'service_timeout', "
        "result_detail = "
        "'lockpick processing timed out before unlock was observed', "
        "executed_at = NOW() "
        "WHERE status = 'processing' "
        "AND action_key = 'lockpick_trade_item' "
        "AND result_code = 'lockpick_trade_ready' "
        "AND expires_at IS NOT NULL "
        "AND expires_at <= NOW()");

    QueryResult processingLockpickResult =
        CharacterDatabase.Query(
            "SELECT id, player_guid, bot_guid, group_id, action_arg "
            "FROM llm_playerbot_actions "
            "WHERE status = 'processing' "
            "AND action_key = 'lockpick_trade_item' "
            "AND result_code = 'lockpick_trade_ready' "
            "AND (expires_at IS NULL OR expires_at > NOW()) "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT 1");

    if (processingLockpickResult)
    {
        Field* processingFields =
            processingLockpickResult->Fetch();

        uint64 processingId =
            processingFields[0].Get<uint64>();

        uint32 processingPlayerGuid =
            processingFields[1].Get<uint32>();

        uint32 processingBotGuid =
            processingFields[2].Get<uint32>();

        uint32 processingGroupId =
            processingFields[3].IsNull()
                ? 0
                : processingFields[3].Get<uint32>();

        std::string processingActionArg =
            processingFields[4].IsNull()
                ? ""
                : processingFields[4].Get<std::string>();

        uint32 processingItemEntry = 0;

        try
        {
            unsigned long parsedEntry =
                std::stoul(
                    processingActionArg);

            if (
                parsedEntry > 0
                && parsedEntry
                    <= std::numeric_limits<uint32>::max())
            {
                processingItemEntry =
                    static_cast<uint32>(
                        parsedEntry);
            }
        }
        catch (...)
        {
            processingItemEntry = 0;
        }

        if (!processingItemEntry)
        {
            CharacterDatabase.DirectExecute(
                "UPDATE llm_playerbot_actions "
                "SET status = 'failed', "
                "result_code = 'invalid_item_entry', "
                "result_detail = "
                "'reserved lockpick item entry is invalid', "
                "executed_at = NOW() "
                "WHERE id = {} "
                "AND status = 'processing' "
                "AND result_code = 'lockpick_trade_ready'",
                processingId);
        }
        else
        {
            Player* processingPlayer =
                ObjectAccessor::FindPlayer(
                    ObjectGuid::Create<HighGuid::Player>(
                        processingPlayerGuid));

            Player* processingBot =
                ObjectAccessor::FindPlayer(
                    ObjectGuid::Create<HighGuid::Player>(
                        processingBotGuid));

            // Temporary offline state does not immediately cancel
            // the reservation. The processing timeout releases it.
            if (
                processingPlayer
                && processingBot
                && processingPlayer->IsInWorld()
                && processingBot->IsInWorld())
            {
                if (!IsPlayerBot(processingBot))
                {
                    CharacterDatabase.DirectExecute(
                        "UPDATE llm_playerbot_actions "
                        "SET status = 'cancelled', "
                        "result_code = 'not_playerbot', "
                        "result_detail = "
                        "'reserved service bot is no longer a PlayerBot', "
                        "executed_at = NOW() "
                        "WHERE id = {} "
                        "AND status = 'processing' "
                        "AND result_code = 'lockpick_trade_ready'",
                        processingId);
                }
                else
                {
                    Group* processingPlayerGroup =
                        processingPlayer->GetGroup();

                    Group* processingBotGroup =
                        processingBot->GetGroup();

                    bool sameProcessingParty =
                        processingPlayerGroup
                        && processingBotGroup
                        && processingPlayerGroup
                            == processingBotGroup
                        && processingGroupId > 0
                        && processingPlayerGroup->GetGUID()
                            .GetCounter()
                            == processingGroupId;

                    if (!sameProcessingParty)
                    {
                        CharacterDatabase.DirectExecute(
                            "UPDATE llm_playerbot_actions "
                            "SET status = 'cancelled', "
                            "result_code = 'party_broken', "
                            "result_detail = "
                            "'service cancelled because the "
                            "reserved party changed or disbanded', "
                            "executed_at = NOW() "
                            "WHERE id = {} "
                            "AND status = 'processing' "
                            "AND result_code = 'lockpick_trade_ready'",
                            processingId);
                    }
                    else
                    {
                        Player* processingTrader =
                            processingBot->GetTrader();

                        TradeData* processingPlayerTrade =
                            processingPlayer->GetTradeData();

                        bool exactProcessingTrade =
                            processingTrader
                                == processingPlayer
                            && processingPlayerTrade
                            && processingPlayerTrade->GetTrader()
                                == processingBot;

                        if (!exactProcessingTrade)
                        {
                            CharacterDatabase.DirectExecute(
                                "UPDATE llm_playerbot_actions "
                                "SET status = 'cancelled', "
                                "result_code = 'trade_ended', "
                                "result_detail = "
                                "'reserved lockpick trade ended "
                                "before unlock was observed', "
                                "executed_at = NOW() "
                                "WHERE id = {} "
                                "AND status = 'processing' "
                                "AND result_code = 'lockpick_trade_ready'",
                                processingId);
                        }
                        else
                        {
                            Item* processingLockbox =
                                processingPlayerTrade->GetItem(
                                    TRADE_SLOT_NONTRADED);

                            if (
                                !processingLockbox
                                || processingLockbox->GetEntry()
                                    != processingItemEntry)
                            {
                                CharacterDatabase.DirectExecute(
                                    "UPDATE llm_playerbot_actions "
                                    "SET status = 'cancelled', "
                                    "result_code = 'trade_item_changed', "
                                    "result_detail = "
                                    "'Do Not Trade item changed before "
                                    "reserved lockbox unlock was observed', "
                                    "executed_at = NOW() "
                                    "WHERE id = {} "
                                    "AND status = 'processing' "
                                    "AND result_code = 'lockpick_trade_ready'",
                                    processingId);
                            }
                            else if (
                                !processingLockbox->IsLocked())
                            {
                                CharacterDatabase.DirectExecute(
                                    "UPDATE llm_playerbot_actions "
                                    "SET status = 'completed', "
                                    "result_code = 'lockpick_completed', "
                                    "result_detail = "
                                    "'reserved lockbox observed unlocked "
                                    "after native Playerbots trade handling', "
                                    "executed_at = NOW() "
                                    "WHERE id = {} "
                                    "AND status = 'processing' "
                                    "AND result_code = 'lockpick_trade_ready'",
                                    processingId);
                            }
                        }
                    }
                }
            }
        }
    }

    // Requests may be classified asynchronously by Python.
    // Never execute an old physical-world instruction after
    // the player/bot context may have changed.
    CharacterDatabase.DirectExecute(
        "UPDATE llm_playerbot_actions "
        "SET status = 'expired', "
        "result_code = 'expired', "
        "result_detail = 'request expired before execution', "
        "executed_at = NOW() "
        "WHERE status = 'pending' "
        "AND expires_at IS NOT NULL "
        "AND expires_at <= NOW()");

    QueryResult result = CharacterDatabase.Query(
        "SELECT id, player_guid, bot_guid, "
        "source_channel, action_key, action_arg, "
        "target_type, target_guid, target_name, group_id "
        "FROM llm_playerbot_actions "
        "WHERE status = 'pending' "
        "AND (expires_at IS NULL OR expires_at > NOW()) "
        "AND (claimed_at IS NULL "
        "OR claimed_at <= NOW() - INTERVAL 3 SECOND) "
        "ORDER BY COALESCE(claimed_at, created_at) ASC, id ASC "
        "LIMIT 1");

    if (!result)
        return;

    Field* fields = result->Fetch();

    uint64 actionId = fields[0].Get<uint64>();
    uint32 playerGuid = fields[1].Get<uint32>();
    uint32 botGuid = fields[2].Get<uint32>();
    std::string sourceChannel =
        fields[3].Get<std::string>();
    std::string actionKey =
        fields[4].Get<std::string>();
    std::string actionArg =
        fields[5].IsNull()
            ? ""
            : fields[5].Get<std::string>();
    std::string targetType =
        fields[6].IsNull()
            ? ""
            : fields[6].Get<std::string>();
    uint64 targetGuid =
        fields[7].IsNull()
            ? 0
            : fields[7].Get<uint64>();
    std::string targetName =
        fields[8].IsNull()
            ? ""
            : fields[8].Get<std::string>();
    uint32 queuedGroupId =
        fields[9].IsNull()
            ? 0
            : fields[9].Get<uint32>();

    // Claim before doing any world-state work. There is only
    // one C++ consumer today, but this also protects us if the
    // polling architecture changes later.
    CharacterDatabase.DirectExecute(
        "UPDATE llm_playerbot_actions "
        "SET status = 'processing', claimed_at = NOW() "
        "WHERE id = {} AND status = 'pending'",
        actionId);

    auto fail = [actionId](
        std::string const& code,
        std::string const& detail)
    {
        CharacterDatabase.DirectExecute(
            "UPDATE llm_playerbot_actions "
            "SET status = 'failed', "
            "result_code = '{}', "
            "result_detail = '{}', "
            "executed_at = NOW() "
            "WHERE id = {}",
            EscapeString(code),
            EscapeString(detail),
            actionId);
    };

    ObjectGuid playerObjectGuid =
        ObjectGuid::Create<HighGuid::Player>(playerGuid);
    ObjectGuid botObjectGuid =
        ObjectGuid::Create<HighGuid::Player>(botGuid);

    Player* player =
        ObjectAccessor::FindPlayer(playerObjectGuid);
    Player* bot =
        ObjectAccessor::FindPlayer(botObjectGuid);

    if (!player || !player->IsInWorld())
    {
        fail(
            "player_offline",
            "requesting player is not online/in world");
        return;
    }

    if (!bot || !bot->IsInWorld())
    {
        fail(
            "bot_offline",
            "requested bot is not online/in world");
        return;
    }

    if (!IsPlayerBot(bot))
    {
        fail(
            "not_playerbot",
            "requested bot guid is not a PlayerBot");
        return;
    }

    // A physical interaction cannot cross maps. This is a
    // hard gate independent of chat channel: whisper/guild/
    // General may reach somebody remotely, but gameplay
    // actions do not become remote merely because chat is.
    if (player->GetMapId() != bot->GetMapId())
    {
        fail(
            "different_map",
            "player and bot are on different maps");
        return;
    }

    // These are the initial action families that always
    // require physical proximity. Exact spell, trade, LOS,
    // alive/dead, mana, cooldown, etc. checks will remain
    // PlayerBots' responsibility once execution is enabled.
    bool localAction =
        actionKey == "cast_spell"
        || actionKey == "rebuff"
        || actionKey == "give_item"
        || actionKey == "trade"
        || actionKey == "heal"
        || actionKey == "resurrect"
        || actionKey == "dispel"
        || actionKey == "crowd_control";

    if (
        localAction
        && !player->IsWithinDistInMap(bot, 100.0f)
    )
    {
        fail(
            "out_of_range",
            "player and bot are not locally reachable");
        return;
    }

    // First live gameplay milestone: only explicitly
    // allowlisted buffs may execute. Every other generic
    // action key remains disabled independently of Python.
    if (
        actionKey != "cast_spell"
        && actionKey != "rebuff")
    {
        fail(
            "action_disabled",
            "gameplay execution is not enabled for this action");
        return;
    }

    bool allowedChannel =
        sourceChannel == "say"
        || sourceChannel == "whisper"
        || sourceChannel == "party"
        || sourceChannel == "raid";

    if (!allowedChannel)
    {
        fail(
            "channel_disabled",
            "buff execution is not enabled for this channel");
        return;
    }

    if (bot->IsInCombat())
    {
        // Combat is a temporary condition for an explicitly
        // requested buff. Return the request to pending so the
        // normal consumer can revalidate and retry after combat.
        CharacterDatabase.DirectExecute(
            "UPDATE llm_playerbot_actions "
            "SET status = 'pending', "
            "claimed_at = NOW(), "
            "result_code = 'waiting_combat', "
            "result_detail = "
            "'buff request deferred until the bot leaves combat' "
            "WHERE id = {} "
            "AND status = 'processing'",
            actionId);
        return;
    }

    if (
        sourceChannel == "party"
        || sourceChannel == "raid")
    {
        Group* playerGroup = player->GetGroup();
        Group* botGroup = bot->GetGroup();

        bool exactQueuedGroup =
            playerGroup
            && botGroup
            && playerGroup == botGroup
            && queuedGroupId > 0
            && playerGroup->GetGUID().GetCounter()
                == queuedGroupId;

        if (!exactQueuedGroup)
        {
            fail(
                "group_changed",
                "player and bot are no longer in the queued group");
            return;
        }
    }

    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);

    if (!ai)
    {
        fail(
            "playerbot_ai_missing",
            "requested PlayerBot has no active AI");
        return;
    }

    bool nativeAccepted = false;
    std::string successDetail;

    if (actionKey == "cast_spell")
    {
        if (
            targetType != "player"
            || targetGuid != playerGuid)
        {
            fail(
                "invalid_buff_target",
                "specific buff target must be the requesting player");
            return;
        }

        std::string nativeSpell;

        if (actionArg == "Power Word: Fortitude")
            nativeSpell = "power word: fortitude";
        else if (actionArg == "Arcane Intellect")
            nativeSpell = "arcane intellect";
        else if (actionArg == "Mark of the Wild")
            nativeSpell = "mark of the wild";
        else if (actionArg == "Blessing of Kings")
            nativeSpell = "blessing of kings";
        else if (actionArg == "Blessing of Wisdom")
            nativeSpell = "blessing of wisdom";
        else if (actionArg == "Blessing of Might")
            nativeSpell = "blessing of might";
        else if (actionArg == "Water Breathing")
            nativeSpell = "water breathing";
        else if (actionArg == "Unending Breath")
            nativeSpell = "unending breath";
        else
        {
            fail(
                "buff_not_allowlisted",
                "specific buff is not in the C++ safe allowlist");
            return;
        }

        std::string nativeParam =
            nativeSpell + " on " + player->GetName();

        Event nativeEvent(
            "llm chatter buff",
            nativeParam,
            player);

        // Clear stale native failure state before this
        // synchronous Playerbots cast attempt.
        ai->ClearLastSpellCastResult();

        nativeAccepted = ai->DoSpecificAction(
            "cast custom spell",
            nativeEvent,
            true);

        successDetail =
            "native Playerbots accepted allowlisted buff "
            + actionArg + " for " + player->GetName();
    }
    else
    {
        if (
            !actionArg.empty()
            || targetType != "none"
            || targetGuid != 0)
        {
            fail(
                "invalid_rebuff_payload",
                "rebuff must not contain a spell or exact target");
            return;
        }

        Event nativeEvent(
            "llm chatter rebuff",
            "",
            player);

        nativeAccepted = ai->DoSpecificAction(
            "force rebuff",
            nativeEvent,
            true);

        successDetail =
            "native Playerbots accepted force rebuff";
    }

    if (!nativeAccepted)
    {
        // Only cast_spell owns a single synchronous spell
        // attempt whose Spell::prepare() result is meaningful
        // here. A broad rebuff may involve different actions
        // and therefore retains the generic failure result.
        if (actionKey == "cast_spell")
        {
            int32 const spellResult =
                ai->GetLastSpellCastResult();

            switch (spellResult)
            {
                case SPELL_FAILED_REAGENTS:
                    fail(
                        "missing_reagent",
                        "native spell cast failed because the "
                        "bot is missing a required reagent");
                    return;

                case SPELL_FAILED_NO_POWER:
                    fail(
                        "no_power",
                        "native spell cast failed because the "
                        "bot does not have enough power");
                    return;

                case SPELL_FAILED_OUT_OF_RANGE:
                    fail(
                        "out_of_range",
                        "native spell cast failed because the "
                        "requesting player is out of range");
                    return;

                case SPELL_FAILED_NOT_READY:
                    fail(
                        "cooldown",
                        "native spell cast failed because the "
                        "spell is not ready");
                    return;

                case SPELL_FAILED_LINE_OF_SIGHT:
                    fail(
                        "line_of_sight",
                        "native spell cast failed because the "
                        "target is not in line of sight");
                    return;

                case SPELL_FAILED_BAD_TARGETS:
                case SPELL_FAILED_BAD_IMPLICIT_TARGETS:
                    fail(
                        "invalid_target",
                        "native spell cast failed because the "
                        "target is invalid for that spell");
                    return;

                default:
                    break;
            }
        }

        fail(
            "native_action_failed",
            "native Playerbots rejected or could not execute "
            "the allowlisted buff request");
        return;
    }

    CharacterDatabase.DirectExecute(
        "UPDATE llm_playerbot_actions "
        "SET status = 'completed', "
        "result_code = 'native_accepted', "
        "result_detail = '{}', "
        "executed_at = NOW() "
        "WHERE id = {} AND status = 'processing'",
        EscapeString(successDetail),
        actionId);
}

void DeliverPendingMessagesImpl()
{
    ProcessPendingPlayerbotActionsImpl();

    CharacterDatabase.DirectExecute(
        "UPDATE llm_chatter_messages "
        "SET delivered = 1, delivered_at = NOW() "
        "WHERE delivered = 0 "
        "AND deliver_at < DATE_SUB(NOW(), "
        "INTERVAL 60 SECOND)");

    QueryResult result;
    // Proximity conversations are scheduled with
    // cumulative deliver_at gaps. If one line is
    // delivered late, gate the next line on the
    // previous line's actual delivered_at so the
    // conversation cannot bunch up afterward.
    if (sLLMChatterConfig->_prioritySystemEnable
        && sLLMChatterConfig
               ->_priorityDeliveryOrderEnable)
    {
        // Ambient rows flow through llm_chatter_queue
        // and therefore keep event_id = NULL.
        // Treat them as lowest priority via COALESCE.
        result = CharacterDatabase.Query(
            "SELECT m.id, m.bot_guid, "
            "m.bot_name, m.message, "
            "m.channel, m.emote, "
            "m.npc_spawn_id, m.player_guid, "
            "m.sequence, m.event_id, e.zone_id, "
            "m.group_id, m.delivery_policy, "
            "m.delivery_reason, m.owner_subsystem "
            "FROM llm_chatter_messages m "
            "LEFT JOIN llm_chatter_events e "
            "ON m.event_id = e.id "
            "WHERE m.delivered = 0 "
            "AND m.deliver_at <= NOW() "
            "AND (m.channel NOT IN ('say', 'msay') "
            "OR m.sequence = 0 "
            "OR NOT EXISTS ("
            "SELECT 1 FROM llm_chatter_messages p "
            "WHERE p.event_id = m.event_id "
            "AND p.sequence = m.sequence - 1 "
            "AND (p.delivered = 0 "
            "OR p.delivered_at IS NULL "
            "OR TIMESTAMPDIFF(SECOND, "
            "p.delivered_at, NOW()) < "
            "TIMESTAMPDIFF(SECOND, "
            "p.deliver_at, m.deliver_at)))) "
            "ORDER BY COALESCE(e.priority, 0) "
            "DESC, m.deliver_at ASC LIMIT 1");
    }
    else
    {
        result = CharacterDatabase.Query(
            "SELECT m.id, m.bot_guid, m.bot_name, "
            "m.message, m.channel, m.emote, "
            "m.npc_spawn_id, m.player_guid, "
            "m.sequence, m.event_id, e.zone_id, "
            "m.group_id, m.delivery_policy, "
            "m.delivery_reason, m.owner_subsystem "
            "FROM llm_chatter_messages m "
            "LEFT JOIN llm_chatter_events e "
            "ON m.event_id = e.id "
            "WHERE m.delivered = 0 "
            "AND m.deliver_at <= NOW() "
            "AND (m.channel NOT IN ('say', 'msay') "
            "OR m.sequence = 0 "
            "OR NOT EXISTS ("
            "SELECT 1 FROM llm_chatter_messages p "
            "WHERE p.event_id = m.event_id "
            "AND p.sequence = m.sequence - 1 "
            "AND (p.delivered = 0 "
            "OR p.delivered_at IS NULL "
            "OR TIMESTAMPDIFF(SECOND, "
            "p.delivered_at, NOW()) < "
            "TIMESTAMPDIFF(SECOND, "
            "p.deliver_at, m.deliver_at)))) "
            "ORDER BY m.deliver_at ASC LIMIT 1");
    }

    if (!result)
        return;

    Field* fields = result->Fetch();
    uint32 messageId = fields[0].Get<uint32>();

    // Claim the row immediately to prevent
    // double-delivery on the next poll tick.
    // Final delivered_at is set after send.
    CharacterDatabase.DirectExecute(
        "UPDATE llm_chatter_messages "
        "SET delivered = 1 "
        "WHERE id = {} AND delivered = 0",
        messageId);
    uint32 botGuid = fields[1].Get<uint32>();
    std::string botName =
        fields[2].Get<std::string>();
    std::string message =
        fields[3].Get<std::string>();
    std::string channel =
        fields[4].Get<std::string>();
    std::string emoteName =
        fields[5].IsNull()
            ? ""
            : fields[5].Get<std::string>();
    uint32 npcSpawnId =
        fields[6].IsNull()
            ? 0
            : fields[6].Get<uint32>();
    uint32 playerGuid =
        fields[7].IsNull()
            ? 0
            : fields[7].Get<uint32>();
    uint32 sequence =
        fields[8].IsNull()
            ? 0
            : fields[8].Get<uint32>();
    uint32 eventId =
        fields[9].IsNull()
            ? 0
            : fields[9].Get<uint32>();
    uint32 eventZoneId =
        fields[10].IsNull()
            ? 0
            : fields[10].Get<uint32>();
    uint32 groupId =
        fields[11].IsNull()
            ? 0
            : fields[11].Get<uint32>();
    std::string deliveryPolicy =
        fields[12].IsNull()
            ? ""
            : fields[12].Get<std::string>();
    std::string deliveryReason =
        fields[13].IsNull()
            ? ""
            : fields[13].Get<std::string>();
    std::string ownerSubsystem =
        fields[14].IsNull()
            ? ""
            : fields[14].Get<std::string>();

    // Master General-channel toggle. If General chatter is
    // disabled, deliberately consume any already-queued General
    // rows instead of speaking them. The row was claimed
    // (delivered = 1) above, so returning here drops it without
    // retry — flipping LLMChatter.GeneralChannel.Enable = 0 via
    // .reload config takes effect immediately for pending rows.
    if (ownerSubsystem == "general"
        && !sLLMChatterConfig->_generalChannelEnable)
        return;

    // Master GroupChatter toggle. Party/raid channels are
    // shared by group, raid-boss, and BG chatter, so we gate
    // on owner_subsystem (the authoritative classifier set at
    // insert time) rather than channel. Group-owned rows are
    // consumed when group chatter is disabled; raid/bg rows
    // (tagged 'raid'/'bg') are untouched. Takes effect
    // immediately for pending rows via .reload config.
    if (ownerSubsystem == "group"
        && !sLLMChatterConfig->_useGroupChatter)
        return;

    // Master ProximityChatter toggle. Consume already-queued
    // proximity rows (open-world say/msay) when proximity
    // chatter is disabled, so flipping
    // LLMChatter.ProximityChatter.Enable = 0 via .reload
    // config takes effect immediately for pending rows too.
    if (ownerSubsystem == "proximity"
        && !sLLMChatterConfig->_proxChatterEnable)
        return;

    // Master GuildChatter toggle. Consume already-queued
    // guild rows when guild chatter is disabled, so flipping
    // LLMChatter.GuildChatter.Enable = 0 takes effect
    // immediately for pending rows (otherwise the generic
    // retry path resets delivered = 0 and retries forever).
    if (ownerSubsystem == "guild"
        && !sLLMChatterConfig->_guildChatterEnable)
        return;

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

    // Only mark delivered after a successful
    // send (or if the bot is unavailable and
    // retrying would not help).
    bool sent = false;
    bool botUnavailable =
        (channel == "msay")
            ? false
            : !bot || !bot->IsInWorld();

    ObjectGuid playerObjGuid =
        ObjectGuid::Create<HighGuid::Player>(
            playerGuid);
    Player* anchorPlayer =
        ObjectAccessor::FindPlayer(playerObjGuid);

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

                if (eventId > 0)
                {
                    QueryResult evRes =
                        CharacterDatabase.Query(
                            "SELECT target_entry"
                            ", target_guid "
                            "FROM "
                            "llm_chatter_events "
                            "WHERE id = {}",
                            eventId);
                    if (evRes)
                    {
                        Field* ef =
                            evRes->Fetch();
                        uint32 tEntry =
                            ef[0].Get<uint32>();
                        uint32 tGuid =
                            ef[1].Get<uint32>();
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
                        QueryResult prevRes =
                            CharacterDatabase.Query(
                                "SELECT bot_guid,"
                                " npc_spawn_id"
                                " FROM llm_chatter_messages"
                                " WHERE event_id = {}"
                                " AND sequence = {}"
                                " LIMIT 1",
                                eventId, sequence - 1);
                        if (prevRes)
                        {
                            Field* pf = prevRes->Fetch();
                            uint32 prevBotGuid =
                                pf[0].IsNull()
                                    ? 0
                                    : pf[0].Get<uint32>();
                            uint32 prevNpcSpawnId =
                                pf[1].IsNull()
                                    ? 0
                                    : pf[1].Get<uint32>();
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

            if (channel == "party")
            {
                Group* grp = bot->GetGroup();
                if (grp && grp->isRaidGroup())
                {
                    SendPartyMessageInstant(
                        bot, grp, processedMessage,
                        "");
                    sent = true;
                }
                else
                {
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
                    grp->BroadcastPacket(
                        &data, false);
                    sent = true;
                }
            }
            else if (channel == "say")
            {
                sent = ai->Say(processedMessage);
            }
            else if (channel == "whisper")
            {
                // Direct player-to-bot conversation reply.
                // player_guid is the authoritative recipient
                // stored on the queued chatter message.
                if (anchorPlayer
                    && anchorPlayer->IsInWorld()
                    && !IsPlayerBot(anchorPlayer))
                {
                    bot->Whisper(
                        processedMessage,
                        LANG_UNIVERSAL,
                        anchorPlayer);

                    sent = true;
                }
                else
                {
                    // A missing/offline player is not a
                    // transient delivery failure for a DM.
                    botUnavailable = true;
                }
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
                            emoteTarget
                                ? emoteTarget->GetName()
                                : "";
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
            !speaker || !speaker->IsAlive()
            || speaker->IsInCombat();
        if (speakerUnavailable)
            botUnavailable = true;
        else
        {
            float originalOrientation =
                speaker->GetOrientation();
            bool facingApplied = false;
            if (sLLMChatterConfig->_facingEnable
                && IsSafeForChatterFacing(speaker))
            {
                bool faced = false;
                if (sequence > 0 && eventId > 0)
                {
                    QueryResult prevRes =
                        CharacterDatabase.Query(
                            "SELECT bot_guid,"
                            " npc_spawn_id"
                            " FROM llm_chatter_messages"
                            " WHERE event_id = {}"
                            " AND sequence = {}"
                            " LIMIT 1",
                            eventId, sequence - 1);
                    if (prevRes)
                    {
                        Field* pf = prevRes->Fetch();
                        uint32 prevBotGuid =
                            pf[0].IsNull()
                                ? 0
                                : pf[0].Get<uint32>();
                        uint32 prevNpcSpawnId =
                            pf[1].IsNull()
                                ? 0
                                : pf[1].Get<uint32>();
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
            speaker->Say(
                msayMessage, LANG_UNIVERSAL);
            sent = true;

            if (!emoteName.empty())
            {
                uint32 textEmoteId =
                    GetTextEmoteId(emoteName);
                if (textEmoteId)
                    SendUnitTextEmote(
                        speaker, textEmoteId,
                        anchorPlayer->GetName());
            }

            if (facingApplied
                && sLLMChatterConfig
                    ->_proxChatterFacingResetDelay
                > 0)
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

    if (sent && eventId > 0
        && playerGuid > 0
        && (channel == "say" || channel == "msay"))
    {
        bool replyEligible = true;
        QueryResult pendingRes =
            CharacterDatabase.Query(
                "SELECT 1 FROM llm_chatter_messages "
                "WHERE event_id = {} "
                "AND sequence > {} "
                "AND delivered = 0 LIMIT 1",
                eventId, sequence);
        if (pendingRes)
            replyEligible = false;

        RecordDeliveredProximityLine(
            eventId,
            playerGuid,
            eventZoneId,
            channel == "say" ? botGuid : 0,
            channel == "msay" ? npcSpawnId : 0,
            replyEligible,
            botName,
            message);
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
            message);
    }

    if (sent || botUnavailable)
    {
        CharacterDatabase.DirectExecute(
            "UPDATE llm_chatter_messages "
            "SET delivered = 1, "
            "delivered_at = NOW() "
            "WHERE id = {}",
            messageId);
    }
    else
    {
        // Unclaim and reschedule for retry
        CharacterDatabase.DirectExecute(
            "UPDATE llm_chatter_messages "
            "SET delivered = 0, "
            "deliver_at = DATE_ADD("
            "NOW(), INTERVAL 5 SECOND) "
            "WHERE id = {}",
            messageId);
    }
}
