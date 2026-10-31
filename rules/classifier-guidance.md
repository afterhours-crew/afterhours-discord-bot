# Classifier guidance

Notes for the language model that reviews messages against the community rules.
Edit this file to tune judgement; it never changes the rules themselves.

## Allowed messages (rule null, score 0)

These break no rule even though they mention forbidden things:

- Asking how to connect a legitimately owned copy of the game to Afterhours or
  another community server, or asking for help with the launcher, the server
  address or an install. Example: "how do I point my install at the Afterhours
  server?" is allowed.
- Reminding others of a rule or quoting it, for example "rule 2 means no cracked
  copies, don't ask for them here". Describing what is forbidden is not doing it.
- Reporting a cheat, exploit, bug or rule-breaker to staff so it can be fixed.
- Talking about piracy, cheats or DRM in the abstract, as news, or with
  disapproval, without asking for them, offering them or explaining how.

## Choosing the rule

Pick the single most specific rule:

- Rule 2 (piracy) is only for obtaining or distributing cracked, leaked or
  unlicensed copies, keys, activation or DRM bypasses, or links and names of
  places that provide them. A hint or euphemism counts the same as a direct
  request.
- Rule 3 (cheats) covers cheats, hacks, trainers, aimbots and exploits meant for
  use in online play, including selling or advertising them. A "cracked trainer"
  or "undetected hack" is rule 3, not rule 2.
- Rule 1 (respect) covers insults, slurs, threats and harassment, including
  threats to find, expose or hurt someone. Only when actual personal details are
  posted (an address, phone number, real name tied to an account, private
  messages) does it become rule 7.
- Rule 7 (personal information) is for sharing such details, one's own or
  someone else's, not for threats or insults alone. A public in-game handle or
  country is not personal information.
- Rule 9 (Discord terms) covers organising raids, mass reporting, using alt
  accounts to evade bans, and other platform abuse that no other rule names.

## Scores

Use high scores only when the text clearly breaks a rule. Ambiguous, joking or
quoted text gets a low score. When no rule applies, return a null rule and a
score of 0.
