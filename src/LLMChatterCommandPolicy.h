/*
 * mod-llm-chatter - narrow Playerbots "who" command disambiguation.
 *
 * Playerbots' ExternalEventHelper::ParseChatCommand matches the longest
 * leading trigger name, so "who starts this quest?" reaches its "who"
 * trigger. The companion Playerbots change (Ai/Base/Actions/
 * WhoConversation.h) makes WhoAction decline such sentences. This mirror
 * lets Chatter treat exactly those sentences as conversation. Both
 * implementations are tested against tools/tests/data/who_routing_cases.json.
 *
 * Only a "who" parameter whose first word continues a sentence is
 * conversational. Bare "who", "who <skill>" and any item/quest link stay
 * commands. Header-only so it is unit tested standalone.
 */
#ifndef MOD_LLM_CHATTER_COMMAND_POLICY_H
#define MOD_LLM_CHATTER_COMMAND_POLICY_H

#include <array>
#include <cctype>
#include <string>
#include <string_view>

namespace LLMChatterCommandPolicy
{
inline bool IsConversationalWhoParam(std::string_view param)
{
    static constexpr std::array<std::string_view, 34> kSentenceWords = {
        "starts", "start", "drops", "drop", "dropped", "is", "are", "was", "were", "knows", "know",
        "has", "have", "had", "can", "could", "wants", "want", "needs", "need", "got", "gets",
        "did", "does", "do", "will", "would", "should", "else", "pulled", "pulls", "killed",
        "gives", "sells"};

    size_t const begin = param.find_first_not_of(" \t");
    if (begin == std::string_view::npos)
        return false;
    std::string lower(param.substr(begin));
    for (char& c : lower)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (lower.find("|hitem:") != std::string::npos || lower.find("|hquest:") != std::string::npos)
        return false;
    size_t end = 0;
    while (end < lower.size() && std::isalpha(static_cast<unsigned char>(lower[end])))
        ++end;
    std::string_view const first(lower.data(), end);
    for (std::string_view word : kSentenceWords)
        if (first == word)
            return true;
    return false;
}

// `lowerMessage` is the trimmed, lower-cased chat line.
inline bool IsConversationalWhoQuestion(std::string_view lowerMessage)
{
    if (lowerMessage.size() < 5 || lowerMessage.substr(0, 4) != "who ")
        return false;
    return IsConversationalWhoParam(lowerMessage.substr(4));
}
} // namespace LLMChatterCommandPolicy

#endif
