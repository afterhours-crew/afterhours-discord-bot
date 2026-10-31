# Classifier guidance

Notes for the language model that reviews messages against the community rules.
Edit this file to tune judgement; it never changes the rules themselves.

## Default: no rule

This community preserves and reverse-engineers games. Packet captures, session
tokens, credentials, login flows, decoders, memory dumps and server emulation
of a member's own game are its normal research topics, not security threats.
Whether a capture tool records tokens or credentials is a research question,
and confirming that it does is a research answer; neither breaks any rule.

Most messages are ordinary conversation: questions, answers, opinions, jokes,
progress reports and technical discussion. They get a null rule and a score of
0. A rule applies only when the message itself does the forbidden thing. Naming,
asking about, answering about, reviewing or criticising a tool, mod, cheat,
crack or person is not doing the forbidden thing. Short replies such as "yeah it
does", "no", "thanks" or "nice" are always null; the earlier messages only
explain what a message refers to, they never make a harmless message a
violation.

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
- Asking what a tool or research does, how it works, or whether it stores or
  captures something such as tokens or credentials, and answering such
  questions.

## Choosing the rule

Pick the single most specific rule:

- Rule 2 (piracy) is only for obtaining or distributing cracked, leaked or
  unlicensed copies, keys, activation or DRM bypasses, or links and names of
  places that provide them. A hint or euphemism counts the same as a direct
  request.
- Rule 3 (cheats) covers cheats, hacks, trainers, aimbots and exploits meant for
  use in online play, including selling or advertising them. A "cracked trainer"
  or "undetected hack" is rule 3, not rule 2.
- Rule 1 (respect) needs hostile words aimed at a person: insults, slurs,
  threats and harassment, including threats to find, expose or hurt someone.
  Technical talk about tokens, credentials, captures, packets, accounts or
  security, including whether a tool collects or stores such data, attacks
  nobody and is null. Only when actual personal details are posted (an address,
  phone number, real name tied to an account, private messages) does it become
  rule 7.
- Rule 7 (personal information) is for a message that itself contains such
  details, one's own or someone else's, not for threats or insults alone and
  not for discussing what data a tool handles. A public in-game handle or
  country is not personal information.
- Rule 8 (credit) needs all three: the message posts, uploads or offers a copy
  of some work; the work is someone else's; and it is presented without credit
  or permission, or as the poster's own. A message that posts nothing (a
  question, an answer, an opinion, a pointer to the original author) fails the
  first test and is null. Members sharing their own research, findings, notes
  or progress never need credit; that is null.
- Rule 9 (Discord terms) covers organising raids, mass reporting, using alt
  accounts to evade bans, and other platform abuse that no other rule names.

## Worked examples

- "anyone know if the packet logger keeps the session token?" → null, 0.
- "it does, there is a flag to strip it" → null, 0.
- "the editor is on the author's github, link is pinned" → null, 0.
- "grabbed the sound mod from the other server and put it up here as mine" →
  rule 8, 0.9.
- "someone was teleporting in the test lobby, flagging it for staff" → null, 0.

## Scores

Use high scores only when the text clearly breaks a rule. Ambiguous, joking or
quoted text gets a low score. When no rule applies, return a null rule and a
score of 0.
