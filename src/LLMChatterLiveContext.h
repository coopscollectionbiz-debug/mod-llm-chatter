/*
 * mod-llm-chatter - live, audience-scoped observations for reactive replies.
 *
 * Captured on the world thread at reactive ingress (Party and whisper),
 * bounded to the turn's eligible responders, and revalidated at delivery.
 */
#ifndef MOD_LLM_CHATTER_LIVE_CONTEXT_H
#define MOD_LLM_CHATTER_LIVE_CONTEXT_H

#include <string>
#include <vector>

class Player;

// True when the turn asks about rare creatures, so the single bounded grid
// search for visible rares is worth doing. Deterministic and cheap.
bool MessageMentionsRare(std::string const& message);

// JSON object member `"live":{...}` (no surrounding braces) describing the
// anchor's world copy, each eligible bot's own state and, if requested,
// rares those bots can currently see. Empty when capture is disabled.
std::string BuildReactiveLiveJson(Player* anchor, std::vector<Player*> const& bots, bool scanRares);

// Revalidates the claims a generated reply relied on against the speaker's
// current state. `reason` names the first failed claim.
bool ReactiveClaimsHold(std::string const& claims, Player* bot, std::string& reason);

#endif
