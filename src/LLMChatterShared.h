#ifndef MOD_LLM_CHATTER_SHARED_H
#define MOD_LLM_CHATTER_SHARED_H

#include "DatabaseEnvFwd.h"
#include "Define.h"
#include "SharedDefines.h"
#include <ctime>
#include <map>
#include <string>
#include <vector>

class Channel;
class Creature;
class Group;
class Map;
class ObjectGuid;
class Player;
class Unit;

enum class LLMChatterPriorityBand : uint8
{
    Filler = 0,
    Normal = 10,
    High = 20,
    // Intra-tier ordering values such as 21 are kept in the
    // implementation file for narrow local ordering cases.
    Critical = 30
};

bool IsPlayerBot(Player* player);
bool IsInOverworld(Player* player);
bool IsGroupedWithRealPlayer(Player* player);
void RefreshGeneralAudienceSnapshot();
bool HasCachedGeneralAudience(
    uint32 mapId, uint32 zoneId, TeamId teamId);
std::vector<uint32> GetCachedGeneralAudienceZones();
// Real-player faction counts for an overworld zone, taken from the
// same snapshot pass as the General audience (no extra session scan).
void GetCachedRealPlayerFactionCounts(
    uint32 zoneId, uint32& allianceCount, uint32& hordeCount);

// World-thread async database callbacks owned by this module. Callbacks
// run only from ProcessLLMChatterAsyncCallbacks(), which the world
// script calls from OnUpdate, so they may touch game objects.
QueryCallbackProcessor& GetLLMChatterQueryProcessor();
void ProcessLLMChatterAsyncCallbacks();
// Fire-and-forget write: async when Delivery.AsyncEnable = 1,
// otherwise the legacy blocking DirectExecute. Pass final SQL only.
void ExecuteLLMChatterWrite(std::string const& sql);
void SetLLMChatterAsyncShutdown();
bool IsLLMChatterAsyncShutdown();
std::string const& GetCreatureEntryColumn();
Creature* FindCreatureBySpawnId(Map* map, uint32 spawnId);
void LoadNamedBossCache();
bool IsLLMChatterBoss(Creature const* creature);
bool IsLLMChatterInternalCreature(Creature const* creature);

// Strip invalid UTF-8 byte sequences, preserving every
// valid UTF-8 run. Returns input unchanged when already
// valid. Used as the universal guard before any SQL/JSON
// write so invalid bytes (e.g. 0xFF) cannot reach MySQL.
std::string SanitizeUtf8(const std::string& str);

// SanitizeUtf8 + codepoint-safe truncation to maxChars
// (0 = no truncation). Single entry point for all
// length-clamped, DB-bound text.
std::string NormalizeChatTextForDb(
    const std::string& str, size_t maxChars = 0);

std::string EscapeString(const std::string& str);
std::string JsonEscape(const std::string& str);
std::string GetCreatureRoleName(Creature* creature);
std::string GetChatterClassName(uint8 classId);
std::string GetRaceName(uint8 raceId);
std::string BuildBotIdentityFields(
    Player* player, bool includeRoles = false);
std::string BuildBotStateJson(Player* player);
std::string BuildBotTravelStateJson(Player* player);
std::string GetBotTravelContext(Player* player);
std::string GetBotTravelMode(Player* player);
bool HasUnsafeChatterFacingMotion(Unit* unit);
bool IsSafeForChatterFacing(Unit* unit);
void UpdateGroupBotTravelState(Player* player, uint32 groupId = 0);
std::string ConvertAllLinks(const std::string& text);
std::string GetZoneName(uint32 zoneId);
uint32 GetTextEmoteId(const std::string& emoteName);
bool IsBGAllowedEmote(const std::string& emoteName);
void PlayUnitTextEmoteAnimation(Unit* unit, uint32 textEmoteId);
void SendUnitTextEmote(Unit* unit, uint32 textEmoteId,
                       const std::string& targetName = "");
void SendBotTextEmote(Player* bot, uint32 textEmoteId);
void SendBotTextEmote(Player* bot, uint32 textEmoteId,
                      const std::string& targetName);

std::string GetTextEmoteName(uint32 emoteId);
void SendPartyMessageInstant(
    Player* bot, Group* group,
    const std::string& message,
    const std::string& emote);
void RecordPartyChatGateActivity(
    uint32 groupId,
    const std::string& deliveryPolicy,
    const std::string& deliveryReason);
Channel* EnsureBotInChatChannel(
    Player* bot, uint32 channelId);
void EnsureBotInGeneralChannel(Player* bot);
std::string PartyReactiveAudience(Group* group, ObjectGuid member);
void InvalidatePartyReactiveAudience(Group* group);
void NotePartyReactiveTurn(uint32 groupId);
bool HasRecentPartyReactiveTurn(uint32 groupId, uint32 seconds);
std::string GeneralReactiveAudience(Player* player, std::string const& channelName);
std::string NewReactiveTurn();
// Worldserver process identity embedded in every reactive audience/turn.
std::string const& ReactiveProcessEpochId();
std::string GetChatterBotRoleName(Player* player);
void StoreReactiveChat(std::string const& audience, Player* speaker, bool isBot, std::string const& message);
bool CanSpeakInGeneralChannel(Player* bot);
bool IsEventOnCooldown(
    std::map<std::string, time_t>& cooldownCache,
    const std::string& cooldownKey,
    uint32 cooldownSeconds);
bool IsPersistedEventOnCooldown(
    std::string const& cooldownKey,
    uint32 cooldownSeconds);
void SetEventCooldown(
    std::map<std::string, time_t>& cooldownCache,
    const std::string& cooldownKey);
uint32 SelectConversationParticipantCount(
    uint32 available, uint32 maximum);
void LogIgnoredAddonChat(
    Player const* player, uint32 type,
    std::string const& msg, char const* source);
// NOTE: extraData must already be valid JSON text and SQL-safe for
// direct insertion into a single-quoted SQL string literal.
void QueueChatterEvent(
    const std::string& eventType,
    const std::string& eventScope,
    uint32 zoneId, uint32 mapId, uint8 priority,
    const std::string& cooldownKey,
    uint32 subjectGuid, const std::string& subjectName,
    uint32 targetGuid, const std::string& targetName,
    uint32 targetEntry, const std::string& extraData,
    uint32 reactAfterSeconds,
    uint32 expiresAfterSeconds,
    bool nullZeroNumeric);
void AppendRaidContext(Player* player, std::string& json);
std::string BuildGroupExperienceFields(Group* group, Map const* map);
bool GroupHasBots(Group* group);
Player* FindMentionedMember(
    Player* bot, Group* grp,
    const std::string& message);
Player* FindNearbyDefenderBot(
    Player* intruder, uint32 zoneId,
    TeamId defenderTeam);
uint8 GetChatterEventPriority(
    const std::string& eventType);
uint32 GetReactionDelaySeconds(const std::string& eventType);
void AddLLMChatterPlayerScripts();
void AddLLMChatterWorldScripts();

#endif
