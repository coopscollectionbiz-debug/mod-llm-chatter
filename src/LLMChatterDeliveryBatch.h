/*
 * mod-llm-chatter - pure delivery batch selection policy
 *
 * Header-only and free of game/database types so the policy can be
 * unit tested without a worldserver. The caller supplies candidates in
 * SQL order (priority DESC, deliver_at ASC) and receives the indexes
 * that may be delivered during the current tick.
 */

#ifndef MOD_LLM_CHATTER_DELIVERY_BATCH_H
#define MOD_LLM_CHATTER_DELIVERY_BATCH_H

#include <cstdint>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace LLMChatterDeliveryBatch
{
struct Candidate
{
    // Unique message row id.
    uint32_t messageId = 0;
    // One line per conversation per tick. Empty means the row is
    // standalone. Usually "e<event_id>" or "q<queue_id>".
    std::string conversationKey;
    // One line per chat destination per tick, e.g. a group, a guild,
    // a General channel or a local /say anchor.
    std::string destinationKey;
    // Channel family used for the per-channel cap ("party", "general").
    std::string channel;
};

struct Limits
{
    // Maximum rows delivered in one tick. Zero is treated as one.
    uint32_t maxBatch = 1;
    // Maximum rows of one channel family in one tick. Zero disables.
    uint32_t maxPerChannel = 0;
};

enum class SkipReason : uint8_t
{
    None,
    BatchFull,
    ConversationBusy,
    DestinationBusy,
    ChannelCap,
    RecentlyHandled,
};

struct Selection
{
    std::vector<size_t> selected;
    // Parallel to the candidate vector; None for selected rows.
    std::vector<SkipReason> reasons;
};

// Greedy selection that keeps SQL priority order. Skipped rows are
// simply left for a later tick; nothing is claimed for them.
inline Selection SelectBatch(
    std::vector<Candidate> const& candidates,
    Limits const& limits,
    std::unordered_set<uint32_t> const& recentlyHandled = {})
{
    Selection result;
    result.reasons.assign(candidates.size(), SkipReason::None);

    uint32_t const maxBatch = limits.maxBatch ? limits.maxBatch : 1;
    std::unordered_set<std::string> conversations;
    std::unordered_set<std::string> destinations;
    std::unordered_set<uint32_t> ids;
    std::unordered_map<std::string, uint32_t> channelCounts;

    for (size_t i = 0; i < candidates.size(); ++i)
    {
        Candidate const& row = candidates[i];

        if (recentlyHandled.count(row.messageId)
            || ids.count(row.messageId))
        {
            result.reasons[i] = SkipReason::RecentlyHandled;
            continue;
        }

        if (result.selected.size() >= maxBatch)
        {
            result.reasons[i] = SkipReason::BatchFull;
            continue;
        }

        if (!row.conversationKey.empty()
            && conversations.count(row.conversationKey))
        {
            result.reasons[i] = SkipReason::ConversationBusy;
            continue;
        }

        if (!row.destinationKey.empty()
            && destinations.count(row.destinationKey))
        {
            result.reasons[i] = SkipReason::DestinationBusy;
            continue;
        }

        if (limits.maxPerChannel
            && channelCounts[row.channel] >= limits.maxPerChannel)
        {
            result.reasons[i] = SkipReason::ChannelCap;
            continue;
        }

        result.selected.push_back(i);
        ids.insert(row.messageId);
        if (!row.conversationKey.empty())
            conversations.insert(row.conversationKey);
        if (!row.destinationKey.empty())
            destinations.insert(row.destinationKey);
        ++channelCounts[row.channel];
    }

    return result;
}

// In-memory guard against re-delivering a row whose terminal database
// write has been queued but not yet applied. Entries expire so a row
// rescheduled for retry becomes eligible again.
class HandledRowGuard
{
public:
    void Mark(uint32_t messageId, int64_t nowSeconds,
        int64_t holdSeconds)
    {
        _until[messageId] = nowSeconds + holdSeconds;
    }

    bool Contains(uint32_t messageId, int64_t nowSeconds) const
    {
        auto itr = _until.find(messageId);
        return itr != _until.end() && itr->second > nowSeconds;
    }

    void Prune(int64_t nowSeconds)
    {
        for (auto itr = _until.begin(); itr != _until.end();)
        {
            if (itr->second <= nowSeconds)
                itr = _until.erase(itr);
            else
                ++itr;
        }
    }

    std::unordered_set<uint32_t> Active(int64_t nowSeconds) const
    {
        std::unordered_set<uint32_t> active;
        for (auto const& [id, until] : _until)
        {
            if (until > nowSeconds)
                active.insert(id);
        }
        return active;
    }

    size_t Size() const { return _until.size(); }

private:
    std::unordered_map<uint32_t, int64_t> _until;
};

// Tracks directed conversations whose remaining lines were cancelled
// by an asynchronous write, so a later fetch cannot deliver them
// before that write is visible.
class CancelledConversationGuard
{
public:
    void Cancel(uint32_t eventId, uint32_t afterSequence,
        int64_t untilSeconds)
    {
        auto& entry = _cancelled[eventId];
        if (entry.untilSeconds == 0
            || afterSequence < entry.afterSequence)
            entry.afterSequence = afterSequence;
        entry.untilSeconds = untilSeconds;
    }

    bool IsCancelled(uint32_t eventId, uint32_t sequence,
        int64_t nowSeconds) const
    {
        if (!eventId)
            return false;
        auto itr = _cancelled.find(eventId);
        return itr != _cancelled.end()
            && itr->second.untilSeconds > nowSeconds
            && sequence > itr->second.afterSequence;
    }

    void Prune(int64_t nowSeconds)
    {
        for (auto itr = _cancelled.begin(); itr != _cancelled.end();)
        {
            if (itr->second.untilSeconds <= nowSeconds)
                itr = _cancelled.erase(itr);
            else
                ++itr;
        }
    }

private:
    struct Entry
    {
        uint32_t afterSequence = 0;
        int64_t untilSeconds = 0;
    };
    std::unordered_map<uint32_t, Entry> _cancelled;
};
} // namespace LLMChatterDeliveryBatch

#endif
