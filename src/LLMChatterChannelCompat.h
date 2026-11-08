/*
 * mod-llm-chatter - portable channel speech eligibility.
 *
 * Patch 1 scoped delivery uses Channel::CanSpeak, which the staging core
 * adds in "Expose channel speech eligibility to scoped delivery"
 * (azerothcore 43e2449cf). Cores without that patch (for example the
 * Release1 source-next tree) do not declare it, and the private member
 * flags cannot be read from a module. This detects the member at compile
 * time so the module builds on both cores:
 *
 *   - patched core: exact membership + mute check, as in Patch 1;
 *   - unpatched core: ChannelSpeechKnown() is false and callers fall back
 *     to Channel::Say's own membership/mute enforcement. A muted bot's line
 *     is then refused by the core without a drop reason, so deployments
 *     should carry the core patch (see CHATTER-2.0-DEPLOYMENT.md).
 */
#ifndef MOD_LLM_CHATTER_CHANNEL_COMPAT_H
#define MOD_LLM_CHATTER_CHANNEL_COMPAT_H

#include <type_traits>
#include <utility>

namespace LLMChatterChannelCompat
{
template <typename C, typename G, typename = void>
struct HasCanSpeak : std::false_type
{
};

template <typename C, typename G>
struct HasCanSpeak<C, G, std::void_t<decltype(std::declval<C const&>().CanSpeak(std::declval<G>()))>>
    : std::true_type
{
};

template <typename C, typename G>
constexpr bool ChannelSpeechKnown()
{
    return HasCanSpeak<C, G>::value;
}

// True when `guid` may speak in `channel`, or when the core cannot tell
// (Channel::Say then enforces membership and mute itself).
template <typename C, typename G>
bool CanSpeakOrUnknown(C const* channel, G const& guid)
{
    if (!channel)
        return false;
    if constexpr (HasCanSpeak<C, G>::value)
        return channel->CanSpeak(guid);
    else
        return true;
}
} // namespace LLMChatterChannelCompat

#endif
