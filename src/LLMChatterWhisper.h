/*
 * mod-llm-chatter - normal-mode whispers from real players to playerbots.
 *
 * Playerbots keeps sole ownership of whisper commands: anything that looks
 * like Playerbots control traffic (including every '@' selector) is ignored
 * here before any history write, event or model call.
 */
#ifndef MOD_LLM_CHATTER_WHISPER_H
#define MOD_LLM_CHATTER_WHISPER_H

#include <string>

class Player;

// Exact sender/recipient pair binding, checked again at delivery.
std::string WhisperReactiveAudience(Player* sender, Player* bot);

void HandlePlayerWhisperToBot(Player* sender, Player* bot, std::string const& message);

#endif
