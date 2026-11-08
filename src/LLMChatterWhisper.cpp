/*
 * mod-llm-chatter - normal-mode whispers from real players to playerbots.
 */

#include "LLMChatterWhisper.h"

#include "LLMChatterConfig.h"
#include "LLMChatterGroupInternal.h"
#include "LLMChatterLiveContext.h"
#include "LLMChatterShared.h"

#include "Player.h"
#include "SocialMgr.h"
#include "StringFormat.h"

#include <chrono>
#include <mutex>
#include <unordered_map>

namespace
{
std::mutex WhisperCooldownMutex;
// (sender << 32 | bot) -> last admitted whisper turn.
std::unordered_map<uint64, std::chrono::steady_clock::time_point> WhisperCooldowns;

std::string Trim(std::string const& input)
{
    size_t start = input.find_first_not_of(" \t\n\r\f\v");
    if (start == std::string::npos)
        return {};
    size_t end = input.find_last_not_of(" \t\n\r\f\v");
    return input.substr(start, end - start + 1);
}

bool CoolingDown(uint64 key, uint32 seconds)
{
    auto const now = std::chrono::steady_clock::now();
    std::lock_guard lock(WhisperCooldownMutex);
    if (WhisperCooldowns.size() > 4096)
    {
        for (auto it = WhisperCooldowns.begin(); it != WhisperCooldowns.end();)
            it = now - it->second > std::chrono::minutes(10) ? WhisperCooldowns.erase(it) : std::next(it);
    }
    auto [it, inserted] = WhisperCooldowns.try_emplace(key, now);
    if (!inserted && now - it->second < std::chrono::seconds(seconds))
        return true;
    it->second = now;
    return false;
}
} // namespace

std::string WhisperReactiveAudience(Player* sender, Player* bot)
{
    if (!sender || !bot)
        return {};
    return Acore::StringFormat("whisper:{}:{}:{}", ReactiveProcessEpochId(),
        sender->GetGUID().GetCounter(), bot->GetGUID().GetCounter());
}

void HandlePlayerWhisperToBot(Player* sender, Player* bot, std::string const& message)
{
    if (!sLLMChatterConfig || !sLLMChatterConfig->IsEnabled() || !sLLMChatterConfig->_whisperChatterEnable)
        return;
    if (!sender || !bot || IsPlayerBot(sender) || !IsPlayerBot(bot) || !sender->IsInWorld() || !bot->IsInWorld())
        return;
    if (sender->GetTeamId() != bot->GetTeamId())
        return;

    std::string text = Trim(message);
    // Playerbots owns every '@' selector and every whisper command. Reject
    // them before history, cooldown, event or lookup work.
    if (text.empty() || text[0] == '@' || text[0] == '.' || text[0] == '#'
        || IsLikelyPlayerbotControlCommand(text) || sLLMChatterConfig->IsPlayerChatPrefixIgnored(text))
        return;

    text = NormalizeChatTextForDb(text, sLLMChatterConfig->_maxMessageLength);
    if (text.empty())
        return;

    uint64 const key = (uint64(sender->GetGUID().GetCounter()) << 32) | bot->GetGUID().GetCounter();
    if (CoolingDown(key, sLLMChatterConfig->_whisperCooldownSeconds))
        return;

    std::string const audience = WhisperReactiveAudience(sender, bot);
    StoreReactiveChat(audience, sender, false, text);

    std::string extraData = "{"
        "\"reactive_audience\":\"" + JsonEscape(audience) + "\","
        "\"reactive_turn\":\"" + NewReactiveTurn() + "\","
        "\"player_name\":\"" + JsonEscape(sender->GetName()) + "\","
        "\"player_message\":\"" + JsonEscape(text) + "\","
        "\"bot_guid\":" + std::to_string(bot->GetGUID().GetCounter()) + ","
        "\"bot_name\":\"" + JsonEscape(bot->GetName()) + "\"";
    std::string const live = BuildReactiveLiveJson(bot, {bot}, MessageMentionsRare(text));
    if (!live.empty())
        extraData += "," + live;
    extraData += "}";

    QueueChatterEvent(
        "bot_player_whisper", "player", bot->GetZoneId(), bot->GetMapId(),
        GetChatterEventPriority("bot_player_whisper"), "",
        sender->GetGUID().GetCounter(), sender->GetName(),
        bot->GetGUID().GetCounter(), bot->GetName(), 0,
        EscapeString(extraData), 0, sLLMChatterConfig->_whisperExpirySeconds, false);
}
