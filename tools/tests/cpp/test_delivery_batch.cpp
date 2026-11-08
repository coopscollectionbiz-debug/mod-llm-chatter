// Standalone tests for src/LLMChatterDeliveryBatch.h.
// Built and run by tools/tests/test_delivery_performance.py; needs only a
// C++17 compiler and no worldserver headers.

#include "LLMChatterDeliveryBatch.h"
#include "LLMChatterDeliveryPolicy.h"

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

using namespace LLMChatterDeliveryBatch;

static int failures = 0;

#define CHECK(cond)                                                     \
    do                                                                  \
    {                                                                   \
        if (!(cond))                                                    \
        {                                                               \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
            ++failures;                                                 \
        }                                                               \
    } while (0)

static Candidate Row(uint32_t id, std::string conversation,
    std::string destination, std::string channel)
{
    Candidate c;
    c.messageId = id;
    c.conversationKey = std::move(conversation);
    c.destinationKey = std::move(destination);
    c.channel = std::move(channel);
    return c;
}

static std::vector<uint32_t> Ids(std::vector<Candidate> const& rows,
    Selection const& selection)
{
    std::vector<uint32_t> ids;
    for (size_t index : selection.selected)
        ids.push_back(rows[index].messageId);
    return ids;
}

static void BatchDeliversSeveralDestinations()
{
    std::vector<Candidate> rows = {
        Row(1, "e1", "group:1", "party"),
        Row(2, "e2", "group:2", "party"),
        Row(3, "q3", "general:0:12", "general"),
        Row(4, "e4", "guild:9", "guild"),
    };
    Limits limits{8, 0};
    Selection s = SelectBatch(rows, limits);
    CHECK((Ids(rows, s) == std::vector<uint32_t>{1, 2, 3, 4}));
}

static void BatchSizeOneMatchesLegacy()
{
    std::vector<Candidate> rows = {
        Row(10, "e1", "group:1", "party"),
        Row(11, "e2", "group:2", "party"),
    };
    Selection s = SelectBatch(rows, Limits{1, 0});
    CHECK((Ids(rows, s) == std::vector<uint32_t>{10}));
    CHECK(s.reasons[1] == SkipReason::BatchFull);

    // Zero is treated as one, never as unlimited.
    Selection zero = SelectBatch(rows, Limits{0, 0});
    CHECK(zero.selected.size() == 1);
}

static void PriorityOrderIsPreserved()
{
    // Input already arrives priority DESC, deliver_at ASC from SQL.
    std::vector<Candidate> rows = {
        Row(30, "e30", "group:1", "party"),   // critical
        Row(31, "e31", "local:5", "say"),     // high
        Row(32, "q32", "general:0:1", "general"), // ambient
    };
    Selection s = SelectBatch(rows, Limits{2, 0});
    CHECK((Ids(rows, s) == std::vector<uint32_t>{30, 31}));
    CHECK(s.reasons[2] == SkipReason::BatchFull);
}

static void OneLinePerConversationPerTick()
{
    // A late conversation whose lines are all due must not bunch up.
    std::vector<Candidate> rows = {
        Row(40, "e7", "general:0:12", "general"),
        Row(41, "e7", "general:0:12", "general"),
        Row(42, "e7", "general:0:12", "general"),
        Row(43, "q8", "general:1:14", "general"),
        Row(44, "q8", "general:1:14", "general"),
    };
    Selection s = SelectBatch(rows, Limits{8, 0});
    CHECK((Ids(rows, s) == std::vector<uint32_t>{40, 43}));
    CHECK(s.reasons[1] == SkipReason::ConversationBusy);
    CHECK(s.reasons[4] == SkipReason::ConversationBusy);
}

static void MultiSpeakerSequenceAdvancesAcrossTicks()
{
    // Three lines of one conversation: first tick takes sequence 0,
    // the next tick (after 0 is marked handled) takes sequence 1.
    std::vector<Candidate> rows = {
        Row(50, "e9", "local:3", "say"),
        Row(51, "e9", "local:3", "say"),
        Row(52, "e9", "local:3", "say"),
    };
    Selection first = SelectBatch(rows, Limits{8, 0});
    CHECK((Ids(rows, first) == std::vector<uint32_t>{50}));

    HandledRowGuard guard;
    guard.Mark(50, 1000, 300);
    std::vector<Candidate> next(rows.begin() + 1, rows.end());
    Selection second = SelectBatch(next, Limits{8, 0},
        guard.Active(1001));
    CHECK((Ids(next, second) == std::vector<uint32_t>{51}));
}

static void OneLinePerDestinationPerTick()
{
    // Two different events in the same party: per-party pacing stays one
    // line per tick, exactly like the legacy global one-line loop.
    std::vector<Candidate> rows = {
        Row(60, "e1", "group:4", "party"),
        Row(61, "e2", "group:4", "party"),
        Row(62, "e3", "group:5", "party"),
    };
    Selection s = SelectBatch(rows, Limits{8, 0});
    CHECK((Ids(rows, s) == std::vector<uint32_t>{60, 62}));
    CHECK(s.reasons[1] == SkipReason::DestinationBusy);
}

static void ChannelCapPreventsMonopoly()
{
    std::vector<Candidate> rows;
    for (uint32_t i = 0; i < 6; ++i)
        rows.push_back(Row(70 + i, "q" + std::to_string(i),
            "general:0:" + std::to_string(i), "general"));
    rows.push_back(Row(80, "e80", "group:1", "party"));
    rows.push_back(Row(81, "e81", "guild:2", "guild"));

    Selection s = SelectBatch(rows, Limits{8, 2});
    CHECK((Ids(rows, s) == std::vector<uint32_t>{70, 71, 80, 81}));
    CHECK(s.reasons[2] == SkipReason::ChannelCap);
}

static void MultiplePlayersProgressTogether()
{
    // Four real players, each with a local scene and a party line.
    std::vector<Candidate> rows;
    for (uint32_t p = 1; p <= 4; ++p)
    {
        rows.push_back(Row(100 + p, "e" + std::to_string(100 + p),
            "local:" + std::to_string(p), "say"));
        rows.push_back(Row(200 + p, "e" + std::to_string(200 + p),
            "group:" + std::to_string(p), "party"));
    }
    Selection s = SelectBatch(rows, Limits{8, 4});
    CHECK(s.selected.size() == 8);
}

static void DuplicatesAreNeverSelected()
{
    std::vector<Candidate> rows = {
        Row(90, "", "msg:90", "general"),
        Row(90, "", "msg:90", "general"), // same row fetched twice
        Row(91, "", "msg:91", "general"),
    };
    HandledRowGuard guard;
    guard.Mark(91, 500, 300);
    Selection s = SelectBatch(rows, Limits{8, 0}, guard.Active(501));
    CHECK((Ids(rows, s) == std::vector<uint32_t>{90}));
    CHECK(s.reasons[1] == SkipReason::RecentlyHandled);
    CHECK(s.reasons[2] == SkipReason::RecentlyHandled);
}

static void GuardExpiresForRetries()
{
    HandledRowGuard guard;
    guard.Mark(7, 100, 4);      // retry hold
    guard.Mark(8, 100, 300);    // delivered hold
    CHECK(guard.Contains(7, 103));
    CHECK(!guard.Contains(7, 104));
    CHECK(guard.Contains(8, 399));
    guard.Prune(104);
    CHECK(guard.Size() == 1);
    guard.Prune(400);
    CHECK(guard.Size() == 0);
}

static void CancelledDirectedConversationStaysCancelled()
{
    CancelledConversationGuard cancelled;
    cancelled.Cancel(12, 1, 400);
    CHECK(!cancelled.IsCancelled(12, 1, 100)); // the dropped line itself
    CHECK(cancelled.IsCancelled(12, 2, 100));
    CHECK(cancelled.IsCancelled(12, 5, 100));
    CHECK(!cancelled.IsCancelled(13, 2, 100));
    CHECK(!cancelled.IsCancelled(0, 2, 100));
    // An earlier drop in the same conversation widens the cancellation.
    cancelled.Cancel(12, 0, 400);
    CHECK(cancelled.IsCancelled(12, 1, 100));
    cancelled.Prune(400);
    CHECK(!cancelled.IsCancelled(12, 2, 400));
}

static void StandaloneRowsDoNotShareConversation()
{
    // Rows without event/queue ids only collide on destination.
    std::vector<Candidate> rows = {
        Row(300, "", "group:1", "party"),
        Row(301, "", "group:2", "party"),
    };
    Selection s = SelectBatch(rows, Limits{8, 0});
    CHECK(s.selected.size() == 2);
}

static void ClockControlledDispatch()
{
    using LLMChatterDeliveryPolicy::CheckDispatch;
    using LLMChatterDeliveryPolicy::DispatchDecision;
    int64_t clockUs = 1000000;
    int64_t const issuedAt = clockUs;
    int64_t const lifetimeUs = 1000000; // One second remaining according to DB.
    CHECK(CheckDispatch(lifetimeUs, clockUs - issuedAt, true) == DispatchDecision::Send);
    clockUs += 999999;
    CHECK(CheckDispatch(lifetimeUs, clockUs - issuedAt, true) == DispatchDecision::Send);
    ++clockUs; // Exactly at the boundary: strict expiry is preserved.
    CHECK(CheckDispatch(lifetimeUs, clockUs - issuedAt, true) == DispatchDecision::Send);
    ++clockUs;
    CHECK(CheckDispatch(lifetimeUs, clockUs - issuedAt, true) == DispatchDecision::Expire);
    // A DB queue wait consumes the same monotonic budget as callback waiting.
    CHECK(CheckDispatch(60000000, 61000000, true) == DispatchDecision::Expire);
    CHECK(CheckDispatch(-1, 0, true) == DispatchDecision::Expire);
    CHECK(CheckDispatch(0, 0, true) == DispatchDecision::Send);
    CHECK(CheckDispatch(0, 1, true) == DispatchDecision::Expire);
    // Shutdown/disable while the query is pending must not dispatch or expire it.
    CHECK(CheckDispatch(lifetimeUs, 0, false) == DispatchDecision::Stop);
    CHECK(CheckDispatch(lifetimeUs, 61000000, false) == DispatchDecision::Stop);
}

static void TestReactiveRouteAndAudience()
{
    using namespace LLMChatterDeliveryPolicy;
    CHECK(ClassifyReactiveRoute("bot_group_general_reaction", "", "party", false) == ReactiveRoute::DropRelay);
    CHECK(ClassifyReactiveRoute("", "bot_group_general_reaction", "party", false) == ReactiveRoute::DropRelay);
    CHECK(ClassifyReactiveRoute("player_general_msg", "", "party", true) == ReactiveRoute::DropUnscoped);
    CHECK(ClassifyReactiveRoute("bot_group_player_msg", "", "party", false) == ReactiveRoute::DropUnscoped);
    CHECK(ClassifyReactiveRoute("player_general_msg", "", "general", true) == ReactiveRoute::General);
    CHECK(ClassifyReactiveRoute("bot_group_player_msg", "", "party", true) == ReactiveRoute::Party);
    CHECK(ClassifyReactiveRoute("", "", "trade", false) == ReactiveRoute::Legacy);

    std::string const party = "party:1:7:3:0:0:11,21,";
    CHECK(MatchesReactiveAudience(party, party, party));
    CHECK(!MatchesReactiveAudience(party, party, "party:1:8:3:0:0:11,21,"));
    CHECK(!MatchesReactiveAudience(party, "party:1:7:4:0:0:11,21,", party));
    CHECK(!MatchesReactiveAudience(party, "party:1:7:3:0:0:11,22,", party));

    std::string const general = "general:1:0:0:12:General - Elwynn Forest";
    CHECK(MatchesReactiveAudience(general, general, general));
    CHECK(!MatchesReactiveAudience(general, general, "general:1:1:0:12:General - Elwynn Forest"));
    CHECK(!MatchesReactiveAudience(general, general, "general:1:0:0:40:General - Westfall"));
    CHECK(CheckDispatch(9, 10, true) == DispatchDecision::Expire);
}

int main(int argc, char** argv)
{
    TestReactiveRouteAndAudience();
    if (argc == 2 && std::string(argv[1]) == "--fetch-sql")
    {
        std::printf("%s\n", LLMChatterDeliveryPolicy::BuildFetchSql(true, 40).c_str());
        return EXIT_SUCCESS;
    }
    if (argc == 5 && std::string(argv[1]) == "--deadline")
    {
        auto decision = LLMChatterDeliveryPolicy::CheckDispatch(
            std::strtoll(argv[2], nullptr, 10), std::strtoll(argv[3], nullptr, 10),
            std::string(argv[4]) == "1");
        using LLMChatterDeliveryPolicy::DispatchDecision;
        std::printf("%s\n", decision == DispatchDecision::Send ? "send"
            : decision == DispatchDecision::Expire ? "expire" : "stop");
        return EXIT_SUCCESS;
    }
    ClockControlledDispatch();
    BatchDeliversSeveralDestinations();
    BatchSizeOneMatchesLegacy();
    PriorityOrderIsPreserved();
    OneLinePerConversationPerTick();
    MultiSpeakerSequenceAdvancesAcrossTicks();
    OneLinePerDestinationPerTick();
    ChannelCapPreventsMonopoly();
    MultiplePlayersProgressTogether();
    DuplicatesAreNeverSelected();
    GuardExpiresForRetries();
    CancelledDirectedConversationStaysCancelled();
    StandaloneRowsDoNotShareConversation();

    if (failures)
    {
        std::printf("%d check(s) failed\n", failures);
        return EXIT_FAILURE;
    }
    std::printf("delivery batch policy: all checks passed\n");
    return EXIT_SUCCESS;
}
