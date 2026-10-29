#ifndef MOD_LLM_CHATTER_GUILD_H
#define MOD_LLM_CHATTER_GUILD_H

#include "Define.h"

#include <string>

void AddLLMChatterGuildScripts();

void NoteGuildPlayerInteraction(uint32 guildId);

bool WasGuildPlayerInteractionRecent(
    uint32 guildId, uint32 seconds);

void UpdatePendingGuildLoginGreetings();

void RecordDeliveredGuildLine(
    uint32 guildId,
    uint32 eventId,
    uint32 botGuid,
    std::string const& botName,
    std::string const& message,
    // Event type already known to the caller (delivery joins it);
    // nullptr looks it up with a blocking query.
    std::string const* knownEventType = nullptr);

#endif
