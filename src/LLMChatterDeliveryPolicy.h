/*
 * mod-llm-chatter - delivery deadlines and indexed conversation dependencies.
 * Shared by world-thread delivery and the standalone offline fixtures.
 */
#ifndef MOD_LLM_CHATTER_DELIVERY_POLICY_H
#define MOD_LLM_CHATTER_DELIVERY_POLICY_H

#include <cstdint>
#include <string>
#include <string_view>

namespace LLMChatterDeliveryPolicy
{
enum class ReactiveRoute
{
    Legacy,
    Party,
    General,
    Whisper,
    DropRelay,
    DropUnscoped
};

inline ReactiveRoute ClassifyReactiveRoute(std::string_view event, std::string_view reason,
    std::string_view channel, bool hasScope)
{
    if (event == "bot_group_general_reaction" || reason == "bot_group_general_reaction")
        return ReactiveRoute::DropRelay;
    bool const party = event == "bot_group_player_msg" || reason == "bot_group_player_msg";
    bool const general = event == "player_general_msg" || reason == "player_general_msg";
    bool const whisper = event == "bot_player_whisper" || reason == "bot_player_whisper";
    // A whisper row must never fall into the legacy General catch-all.
    if (!party && !general && !whisper)
        return channel == "whisper" ? ReactiveRoute::DropUnscoped : ReactiveRoute::Legacy;
    if (!hasScope || int(party) + int(general) + int(whisper) != 1)
        return ReactiveRoute::DropUnscoped;
    if (party)
        return channel == "party" ? ReactiveRoute::Party : ReactiveRoute::DropUnscoped;
    if (general)
        return channel == "general" ? ReactiveRoute::General : ReactiveRoute::DropUnscoped;
    return channel == "whisper" ? ReactiveRoute::Whisper : ReactiveRoute::DropUnscoped;
}

inline bool MatchesReactiveAudience(std::string_view captured, std::string_view source, std::string_view speaker)
{
    return !captured.empty() && captured == source && captured == speaker;
}

enum class DispatchDecision
{
    Send,
    Expire,
    Stop
};

// The database returns a signed lifetime at statement start, not an absolute
// timestamp. Charge ALL monotonic time since submission against it, including
// time waiting for a DB worker, executing SQL and waiting for the world tick.
// This is conservative under DB backlog and independent of wall-clock skew.
// The boundary itself remains valid, matching the existing strict '<' sweep.
inline DispatchDecision CheckDispatch(
    int64_t remainingUs, int64_t elapsedUs, bool running)
{
    if (!running)
        return DispatchDecision::Stop;
    if (remainingUs < 0 || elapsedUs < 0 || elapsedUs > remainingUs)
        return DispatchDecision::Expire;
    return DispatchDecision::Send;
}

inline std::string BuildFetchSql(bool priorityOrder, uint32_t limit, uint32_t expirySeconds = 60)
{
    std::string const expiry = std::to_string(expirySeconds);
    return
        "SELECT m.id, m.bot_guid, m.bot_name, m.message, "
        "m.channel, m.emote, m.npc_spawn_id, m.player_guid, "
        "m.sequence, m.event_id, e.zone_id, m.group_id, m.delivery_policy, "
        "m.delivery_reason, m.owner_subsystem, m.addressee_player_guid, "
        "m.addressee_bot_guid, m.addressee_npc_spawn_id, "
        "e.map_id, e.extra_data, e.event_type, e.subject_guid, "
        "m.queue_id, e.target_entry, e.target_guid, "
        "(SELECT pv.bot_guid FROM llm_chatter_messages pv "
        "WHERE m.event_id IS NOT NULL AND m.sequence > 0 "
        "AND pv.event_id = m.event_id AND pv.sequence = m.sequence - 1 LIMIT 1) AS prev_bot_guid, "
        "(SELECT pv.npc_spawn_id FROM llm_chatter_messages pv "
        "WHERE m.event_id IS NOT NULL AND m.sequence > 0 "
        "AND pv.event_id = m.event_id AND pv.sequence = m.sequence - 1 LIMIT 1) AS prev_npc_spawn_id, "
        "(m.event_id IS NOT NULL AND EXISTS (SELECT 1 FROM llm_chatter_messages lt "
        "WHERE lt.event_id = m.event_id AND lt.sequence > m.sequence "
        "AND lt.delivered = 0)) AS has_later_line, "
        "TIMESTAMPDIFF(MICROSECOND, NOW(6), CASE "
        "WHEN e.event_type IN ('player_general_msg','bot_group_player_msg') "
        "THEN DATE_SUB(e.expires_at, INTERVAL 1 MICROSECOND) "
        "ELSE DATE_ADD(m.deliver_at, INTERVAL " + expiry + " SECOND) END) AS remaining_us, "
        "JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(e.extra_data),e.extra_data,'{}'),"
        "'$.reactive_audience')) AS reactive_audience, "
        "JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(e.extra_data),e.extra_data,'{}'),"
        "'$.reactive_turn')) AS reactive_turn, "
        // Volatile claims a reply relied on, revalidated before send.
        "m.claim_json "
        "FROM llm_chatter_messages m LEFT JOIN llm_chatter_events e ON m.event_id = e.id "
        "WHERE m.delivered = 0 AND m.deliver_at <= NOW(6) "
        "AND m.deliver_at >= DATE_SUB(NOW(6), INTERVAL " + expiry + " SECOND) "
        // idx_event restricts this check to the current conversation. All lower
        // pending sequences block, even if rescheduled or awaiting the sweep.
        // Terminal predecessors release ordinary conversations; directed drops
        // retain the existing cancellation cascade in FinalizeDroppedMessage.
        "AND (m.event_id IS NULL OR NOT EXISTS ("
        "SELECT 1 FROM llm_chatter_messages p WHERE p.event_id = m.event_id "
        "AND p.sequence < m.sequence AND (p.delivered = 0 "
        // Keep the additional actual-delivery spacing for local conversations.
        "OR (m.channel IN ('say', 'msay') AND p.sequence = m.sequence - 1 "
        "AND (p.delivered_at IS NULL OR TIMESTAMPDIFF(MICROSECOND, p.delivered_at, NOW(6)) < "
        "TIMESTAMPDIFF(MICROSECOND, p.deliver_at, m.deliver_at)))))) "
        // Rows with an event id use that identity even if queue_id is also set.
        // idx_queue gives the same bounded check to legacy ambient scripts.
        "AND (m.event_id IS NOT NULL OR m.queue_id IS NULL OR NOT EXISTS ("
        "SELECT 1 FROM llm_chatter_messages qp WHERE qp.queue_id = m.queue_id "
        "AND qp.event_id IS NULL AND qp.sequence < m.sequence AND (qp.delivered = 0 "
        "OR (m.channel IN ('say', 'msay') AND qp.sequence = m.sequence - 1 "
        "AND (qp.delivered_at IS NULL OR TIMESTAMPDIFF(MICROSECOND, qp.delivered_at, NOW(6)) < "
        "TIMESTAMPDIFF(MICROSECOND, qp.deliver_at, m.deliver_at)))))) "
        "ORDER BY " + (priorityOrder ? "COALESCE(e.priority, 0) DESC, " : "") +
        "m.deliver_at ASC, m.id ASC LIMIT " + std::to_string(limit);
}
} // namespace LLMChatterDeliveryPolicy
#endif
