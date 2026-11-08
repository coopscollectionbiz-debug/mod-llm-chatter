// Standalone tests for the Chatter 2.0 header-only policies:
//   src/LLMChatterClaimPolicy.h    (delivery-time live-claim revalidation)
//   src/LLMChatterDeliveryPolicy.h (reactive route classification)
// Built and run by tools/tests/test_chatter2_cpp_policy.py.

#include "LLMChatterClaimPolicy.h"
#include "LLMChatterDeliveryPolicy.h"

#include <cstdio>
#include <cstdlib>
#include <vector>

using namespace LLMChatterClaimPolicy;
using LLMChatterDeliveryPolicy::ClassifyReactiveRoute;
using LLMChatterDeliveryPolicy::ReactiveRoute;

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

static SpeakerState Healer(int32_t manaPct, bool alive = true, bool combat = false)
{
    SpeakerState state;
    state.present = true;
    state.alive = alive;
    state.inCombat = combat;
    state.powerType = 0;
    state.powerPct = manaPct;
    return state;
}

static void ParsesWireFormat()
{
    std::vector<Claim> claims;
    CHECK(ParseClaims("", claims) && claims.empty());
    CHECK(ParseClaims("power_low;g=42;pt=0;th=35", claims) && claims.size() == 1);
    CHECK(claims[0].kind == ClaimKind::PowerLow && claims[0].subject == 42 && claims[0].threshold == 35);
    CHECK(ParseClaims("power_ok;g=7;pt=3;th=35|rare;g=7;s=118811;e=61", claims) && claims.size() == 2);
    CHECK(claims[1].kind == ClaimKind::RareVisible && claims[1].spawnId == 118811 && claims[1].entry == 61);
}

static void MalformedClaimsFailClosed()
{
    std::vector<Claim> claims;
    CHECK(!ParseClaims("power_low", claims));                 // no subject
    CHECK(!ParseClaims("power_low;g=x", claims));             // non-numeric
    CHECK(!ParseClaims("power_low;g=-1", claims));            // sign
    CHECK(!ParseClaims("teleport;g=1", claims));              // unknown kind
    CHECK(!ParseClaims("power_low;g=1;zz=2", claims));        // unknown key
    CHECK(!ParseClaims("rare;g=1;s=5", claims));              // rare without entry
    CHECK(!ParseClaims("power_low;g=99999999999", claims));   // overflow
    CHECK(!ParseClaims("alive;g=1|alive;g=1|alive;g=1|alive;g=1|alive;g=1", claims)); // too many
    std::string huge(600, 'a');
    CHECK(!ParseClaims(huge, claims));
}

static void LowManaClaimFollowsLiveMana()
{
    Claim claim;
    claim.kind = ClaimKind::PowerLow;
    claim.subject = 1;
    claim.threshold = 35;
    CHECK(SpeakerClaimHolds(claim, Healer(20)));
    CHECK(SpeakerClaimHolds(claim, Healer(40)));   // within hysteresis: small regen
    CHECK(!SpeakerClaimHolds(claim, Healer(80)));  // drank to full: "mana sec" is stale
    CHECK(!SpeakerClaimHolds(claim, Healer(20, false))); // died meanwhile
    SpeakerState wrongPower = Healer(20);
    wrongPower.powerType = 1;                       // shapeshifted to rage
    CHECK(!SpeakerClaimHolds(claim, wrongPower));
    SpeakerState absent;
    CHECK(!SpeakerClaimHolds(claim, absent));
}

static void PowerOkClaimCatchesReversal()
{
    Claim claim;
    claim.kind = ClaimKind::PowerOk;
    claim.subject = 1;
    claim.threshold = 35;
    CHECK(SpeakerClaimHolds(claim, Healer(90)));
    CHECK(SpeakerClaimHolds(claim, Healer(30)));   // hysteresis
    CHECK(!SpeakerClaimHolds(claim, Healer(10)));  // went OOM while generating
    CHECK(!SpeakerClaimHolds(claim, Healer(-1)));  // no such power
}

static void CombatAndLifeClaims()
{
    Claim claim;
    claim.subject = 1;
    claim.kind = ClaimKind::OutOfCombat;
    CHECK(SpeakerClaimHolds(claim, Healer(50, true, false)));
    CHECK(!SpeakerClaimHolds(claim, Healer(50, true, true)));
    claim.kind = ClaimKind::InCombat;
    CHECK(SpeakerClaimHolds(claim, Healer(50, true, true)));
    claim.kind = ClaimKind::Dead;
    CHECK(SpeakerClaimHolds(claim, Healer(0, false)));
    CHECK(!SpeakerClaimHolds(claim, Healer(0, true)));
    claim.kind = ClaimKind::Alive;
    CHECK(!SpeakerClaimHolds(claim, Healer(0, false)));
}

static void RareClaimNeedsLiveVisibleSameRare()
{
    Claim claim;
    claim.kind = ClaimKind::RareVisible;
    claim.subject = 1;
    claim.spawnId = 9;
    claim.entry = 61;
    RareState rare;
    rare.found = true;
    rare.alive = true;
    rare.visibleToSpeaker = true;
    rare.entry = 61;
    rare.distance = 40.0f;
    CHECK(RareClaimHolds(claim, rare));
    RareState dead = rare;
    dead.alive = false;
    CHECK(!RareClaimHolds(claim, dead));             // killed meanwhile
    RareState hidden = rare;
    hidden.visibleToSpeaker = false;
    CHECK(!RareClaimHolds(claim, hidden));           // phased/stealthed away
    RareState far = rare;
    far.distance = 150.0f;
    CHECK(!RareClaimHolds(claim, far));
    RareState other = rare;
    other.entry = 62;
    CHECK(!RareClaimHolds(claim, other));            // spawn id reused by another entry
    RareState gone;
    CHECK(!RareClaimHolds(claim, gone));             // despawned
}

static void ReactiveRoutesStayOnTheirChannel()
{
    CHECK(ClassifyReactiveRoute("bot_group_player_msg", "bot_group_player_msg", "party", true) == ReactiveRoute::Party);
    CHECK(ClassifyReactiveRoute("player_general_msg", "", "general", true) == ReactiveRoute::General);
    CHECK(ClassifyReactiveRoute("bot_player_whisper", "bot_player_whisper", "whisper", true) == ReactiveRoute::Whisper);
    // Wrong channel for the captured event: fail closed, never re-route.
    CHECK(ClassifyReactiveRoute("bot_player_whisper", "", "general", true) == ReactiveRoute::DropUnscoped);
    CHECK(ClassifyReactiveRoute("bot_player_whisper", "", "party", true) == ReactiveRoute::DropUnscoped);
    CHECK(ClassifyReactiveRoute("bot_group_player_msg", "", "general", true) == ReactiveRoute::DropUnscoped);
    CHECK(ClassifyReactiveRoute("player_general_msg", "", "party", true) == ReactiveRoute::DropUnscoped);
    // Missing scope or conflicting identities.
    CHECK(ClassifyReactiveRoute("bot_player_whisper", "", "whisper", false) == ReactiveRoute::DropUnscoped);
    CHECK(ClassifyReactiveRoute("bot_group_player_msg", "player_general_msg", "party", true) == ReactiveRoute::DropUnscoped);
    // A whisper row must never reach the legacy General catch-all.
    CHECK(ClassifyReactiveRoute("bot_group_idle", "", "whisper", true) == ReactiveRoute::DropUnscoped);
    // The retired General-to-Party relay stays retired.
    CHECK(ClassifyReactiveRoute("bot_group_general_reaction", "", "party", true) == ReactiveRoute::DropRelay);
    // Unrelated producers keep the legacy path.
    CHECK(ClassifyReactiveRoute("bot_group_kill", "", "party", false) == ReactiveRoute::Legacy);
}

static void FetchSqlCarriesClaims()
{
    std::string const sql = LLMChatterDeliveryPolicy::BuildFetchSql(true, 8);
    CHECK(sql.find("m.claim_json") != std::string::npos);
    CHECK(sql.find("AS reactive_turn, ") != std::string::npos);
}

int main()
{
    ParsesWireFormat();
    MalformedClaimsFailClosed();
    LowManaClaimFollowsLiveMana();
    PowerOkClaimCatchesReversal();
    CombatAndLifeClaims();
    RareClaimNeedsLiveVisibleSameRare();
    ReactiveRoutesStayOnTheirChannel();
    FetchSqlCarriesClaims();
    if (failures)
    {
        std::printf("%d check(s) failed\n", failures);
        return EXIT_FAILURE;
    }
    std::printf("chatter2 policy: all checks passed\n");
    return EXIT_SUCCESS;
}
