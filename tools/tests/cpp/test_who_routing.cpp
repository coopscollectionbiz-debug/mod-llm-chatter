// Executes both C++ copies of the "who" rule against cases on stdin.
// Each line: <0|1><TAB><text>. Built with -I to src/ and to the Playerbots
// Actions directory (defines HAVE_PLAYERBOTS_WHO when that header is given).
#include "LLMChatterCommandPolicy.h"
#ifdef HAVE_PLAYERBOTS_WHO
#include "WhoConversation.h"
#endif

#include <algorithm>
#include <cctype>
#include <iostream>
#include <string>

int main()
{
    std::string line;
    int failures = 0;
    int count = 0;
    while (std::getline(std::cin, line))
    {
        if (!line.empty() && line.back() == '\r')
            line.pop_back();
        if (line.size() < 2 || line[1] != '\t')
            continue;
        bool const expected = line[0] == '1';
        std::string const text = line.substr(2);
        std::string lower = text;
        std::transform(lower.begin(), lower.end(), lower.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        ++count;
        if (LLMChatterCommandPolicy::IsConversationalWhoQuestion(lower) != expected)
        {
            std::cout << "chatter mismatch: " << text << "\n";
            ++failures;
        }
#ifdef HAVE_PLAYERBOTS_WHO
        // Playerbots receives the original-case parameter after "who ".
        if (lower.rfind("who ", 0) == 0 && WhoConversation::IsConversationalParam(text.substr(4)) != expected)
        {
            std::cout << "playerbots mismatch: " << text << "\n";
            ++failures;
        }
        if (lower == "who" && WhoConversation::IsConversationalParam("") != false)
            ++failures;
#endif
    }
    if (failures || !count)
    {
        std::cout << failures << " failures in " << count << " cases\n";
        return 1;
    }
    std::cout << "all " << count << " who cases passed\n";
    return 0;
}
