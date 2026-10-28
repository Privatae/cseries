"""Go-ahead guard — a turn never ends asking the user for a go-ahead.

Origin: replies that ended like

    "…Say go, or swap anything you don't like."
    "…then you can approve the phase in the Workbench — say build and
     I'll kick that off."
    "…One more build on that phase to patch those two lines, then the
     queued items run. Say go and I'll kick the fix."

and each time the user had to type one word to get the work they had already
asked for. That word starts a NEW turn: the context is rebuilt from cold and
the model re-reads everything to do what the last turn could have done. A
user must never read "say go": a turn that ends asking for a one-word response
resets the turn.

The pod log showed the mechanism: k3 sends the reply text and `close_turn` in
one completion, `close_turn.is_terminal` makes that completion the end of the
turn, and the turn leaves through the ordinary text exit, where nothing looked
at what the reply asked for.

This module is the check at that exit (session.py's _go_ahead_round runs it
on the plain text exit, the terminal close_turn path and the narrate-first
dispatch exit, once per turn, beside the claim guard). It is deterministic, never a model call, and it holds the SAME rule
as the end-to-end test's ship gate (ported below, with the gate's cases run through it
in scripts/test_go_ahead_guard.py): the default is "ask", and a closing
sentence gets out only through a structural exception. It is an ask when the
Host offers its own further work, makes its next step wait on the user, asks
for a reply word, leaves a question unanswered anywhere in the reply, or leads
a list with an offer or ask.

The guard's own additions, outside the rule:
  - in a turn with a card open in front of the person (an `escalate`
    ticket, a platform card, a phase's approval card — card_from_tool),
    every closing line that asks counts, at any level, and the correction
    names that card instead of asking for another; one repair per turn,
    and the repair's own text is checked against the
    cards open before it is sent (repair_text);
  - a close_turn handoff that records a wait ("Awaiting user go") is caught
    like the reply (find_go_ahead_wait), since the next turn starts from it.

A caught reply costs one model round with a correction that also says what to
do when there is nothing to do ("leave the offer out"), and the guard fires at
most once a turn.
"""
from __future__ import annotations

import re

# ── what an ask IS: the ship gate's rule, ported verbatim ────────────────────
# Source: the ship gate's go_ahead.py — everything from its "what an ask IS" banner to its
# verdict section, unchanged below, so the gate and this guard give the SAME
# verdict on the same text (scripts/test_go_ahead_guard.py runs the gate's
# whole detector case list through the guard). The harness cannot import this
# module and this module cannot import it; if one changes, change the other.
# The guard's own additions are outside the rule: an open card changes the
# correction, never what counts (correction, card_from_tool), and a close_turn
# handoff that records a wait is caught (find_go_ahead_wait). The
# rule's find_ask label carries its LEVEL — `core` ⊂ `explicit` ⊂ any (the rest
# is `judged`) — as `.level` / `.core` / `.explicit`; the guard reads it to
# decide which answers the correction offers, and records it per firing.

# ------------------------------------------------------ what an ask IS ---
#
# THE RULE: a Host never ends a turn waiting on a user
# reply or a go-ahead, or offering work instead of doing it. An ask is a
# sentence of the Host's own that leaves something open for the user to
# answer. The default is ASK; only the exceptions below let a sentence out.
#
# SCOPE: the whole reply. An ask anywhere in the Host's prose that is still
# open when the turn ends is what the user is left with. Only two things close
# one: a real answer right after a fact question, and the words being someone
# else's (code, a quotation, a draft message).
#
# What an ask does:
#   (a) OFFERS WORK INSTEAD OF DOING IT. Willingness always proposes (happy /
#       glad / ready / keen to, I'd love to, let's + a verb of work — "let's"
#       with its apostrophe; "lets you" is a statement). Ability
#       (I / we can, could, am able to) proposes only when it is for the future
#       — marked "also / too / next / then / go ahead / for you …", tied to the
#       user's wish or reply — and not a description of what the Host can do
#       (a result or reason after it: "so it went to the root", a contrast, a
#       generality, a list of abilities). "If you want me to …" always offers.
#   (b) MAKES A HOST STEP WAIT ON THE USER: a request to reply, decide, confirm
#       or provide (an imperative of telling, deciding or supplying, recognised
#       by where it stands in the sentence) — with the Host's step after it, a
#       wait by default ("if that's accurate, say so and I'll redispatch", "if
#       it looks good, let me know and I'll merge"), except when its condition
#       states a DEFECT in the result and the step does not make PROGRESS on
#       the work ("if anything looks off, tell me and I'll queue another
#       pass": a contingency; "… and I'll commit it": a wait); without a Host
#       step, a condition on the world keeps it a contingency ("if it looks
#       off, tell me"); the user's readiness gating the Host's
#       next step ("whenever you are", "when you're ready: …"); the Host naming
#       input it needs ("one thing I need from you: …"); any other imperative with the
#       Host's step as its payoff ("give it a read and I'll tweak it", "click
#       Approve so I can continue") — recognised only by positive evidence, its
#       verb followed directly by that verb's object, so "Phase 4 is running
#       and I'll report back" is a statement; the decision handed over ("your
#       call", "up to you", "on you now"); the user's reply or input wanted
#       ("your thoughts", "I'd love a second pair of eyes", "for you to review
#       before I …"); the Host's progress waiting on a user signal ("pending a
#       thumbs-up", "thumbs up and I'll publish"); the Host halting ("standing
#       by", "until you confirm", in any tense); or a Host step governed by a
#       condition on the user (CONDITIONS below). A Host step that only reports
#       back ("I'll report the diff when it's reviewed") waits on nothing.
#   (c) ASKS FOR A REPLY: reply / respond / answer with anything; say / type /
#       send / drop a word, however quoted. Asking the user to SAY or TYPE a
#       word so the Host proceeds is the founding case and is read before
#       every way out, a real approval surface included ("approve the phase in
#       the Workbench and say build to continue" asks). Only an explicit subject
#       ("I'd say yes"), a later occasion, another program's prompt with no Host
#       step after it, and the user's own command let it out.
#   (d) ASKS A QUESTION the user must answer. The addressee decides: a question
#       to the user, proposing the Host's own action, asking the user's
#       judgement, or with no stated subject can never be answered by the Host.
#       A fact question is answered by the next sentence when it gives its
#       polarity or value (a yes / no, a negated fact, the fact about its
#       subject, or the queried words). A question to a third party ("Can
#       someone check …?", a message's audience) is not the user's. Small talk
#       about the user asks nothing ("How about you?", "What's on your mind?",
#       "Are you okay?"), checked before any other reading. A tag question asks
#       what the statement it tags asks ("Simple enough, right?" asks
#       nothing). A wh-question that lost its "?" asks when it ends in the
#       Host's payoff ("What's the new number — and I'll lock it in") or ends
#       the reply in interrogative order ("What's first on the list.").
#   (e) LEADS A LIST that offers or asks, read with its items; a next-steps or
#       options lead-in offers only if an item proposes an action.
#
# CONDITIONS decide between (b) and a contingency, by subject, predicate and
# connective:
#   the user decides, wants, evaluates or supplies the Host ("when you're
#   ready", "once you confirm", "if you want", "if you send me the key",
#   "until I hear back")  -> the Host waits on the user;
#   the Host's own receipt of the user's input ("once I have your list")
#     -> the Host waits on the user;
#   the user as the EVALUATOR, whatever the subject ("if it checks out on your
#   end", "if it's to your liking", "if this is what you're after", "the name
#   you picked")  -> a wait when a Host step follows, else a contingency;
#   any other act of the user's waits when the connective sequences the Host
#   after it ("once you're connected, I'll re-fire them"); under "if" it is a
#   contingency ("if you rename it, I'll keep tracking it by its id");
#   an APPROVAL of the work — said of it ("if that works for you", "if the
#   name works"), of anything as a hypothetical benefit ("if a README would
#   help", "if more tests would be useful"), or in the passive ("once it's
#   approved", "when it's been reviewed") — or a wish of anyone but the work
#   itself ("if the team needs a copy"; not "if it needs more memory")
#     -> the Host waits on the user, and "I can" in its consequent is an offer;
#   the world or the result ("if this one dies the same way", "if git
#   complains", "if it looks different from what you expected"), or the user
#   meeting a problem with it ("if you try it and it breaks")
#     -> a CONTINGENCY, which asks nothing — when the connective makes the step
#        happen upon it (if / when / once / as soon as / after); "before /
#        until" set a deadline or a halt instead; a request to the user timed
#        by an expected event ("when the dialog appears, click Approve so I
#        can continue") still asks;
#   the Host's own sequencing ("before I commit", "before pushing"), and
#   hypotheticals ("if I could see your screen, I'd …")  -> never a wait.
#   A clause right after a verb of telling is its object, not a condition:
#   "Tell me if the silk comes up now" asks the user to report; "tell me if
#   anything looks off" (a _DEVIATION) is a contingency. A condition governs its
#   own clause: "I stopped before touching it — say the word …" asks.
#
# THE OTHER WAYS OUT: an invitation to a new request that names no work ("let
#   me know if you need anything else", "ask it directly") when it is the
#   sentence's only request; a later occasion
#   scoping the user's reply ("next time, just say go"); another program's
#   prompt named as the addressee; a pointer to a real place in the product, or
#   to a real approval surface for a real gate, in any mood ("Approve the phase
#   in the Workbench to ship it"); the user doing the work themselves while the
#   Host abstains; a reference to the user's earlier words ("per your
#   instruction, I'm writing it up", "as you asked") — "your X" is input the
#   Host needs only in a frame of need, waiting, receipt or wish ("I need your
#   X", "waiting on your X", "once I have your X", "I'd like your pick"); an
#   ask MENTIONED, not made — a heading line, a reported ask ("you asked me
#   to …", "why I kept asking you to say go"); a label or lead and colon
#   opening a line, whose content is
#   structure, not a request ("On commit: check for danger", "The brief is
#   fix-only: walk every mode", "Review: not needed") unless it is aimed at the
#   Host or paid for with its step; a verb that is an item of a list of single
#   words ("sync, compose, reply, labels"); "let's X" narrating what the reply
#   then reports done ("Let's verify it landed. I read it back …");
#   negated, counterfactual, past and reporting forms; a purpose or relative
#   clause; a user's own command ("Type `cat …` to see it").
#
# DRAFTS: a message to someone else is quotation only with the form of one — a
# salutation, a sign-off, or a recipient named in the lead-in ("email to
# Dana:"). A lead-in word alone (note, message, reply …) is never enough. It
# runs through its sign-off; with none, only its first paragraph, and never the
# reply's last paragraph.
#
# SEGMENTATION: blocks (prose, code, drafts); a trailing run of code and its
# captions is content, unless a caption asks; paragraphs split into units
# (list items, headings, bold lines, blockquote runs, lines starting a new
# sentence) and units into sentences.

_FOLD = {ord("’"): "'", ord("‘"): "'", ord("“"): '"', ord("”"): '"',
         ord("„"): '"', ord("«"): '"', ord("»"): '"',
         ord("？"): "? ", ord("؟"): "? ", ord("。"): ". ", ord("！"): "! ",
         ord("¿"): None, ord("¡"): None}

# --- small grammar helpers ---------------------------------------------------
_OPENS = r"(?:^|[,;:—–(]\s*|\s-\s|\b(?:just|simply|then|and|or|so|please|now|kindly)\s)"
_NEG = re.compile(r"\b(?:no|not|never|nothing|none|nobody|nor)\b|n't\b", re.I)
_CLAUSE_BREAK = re.compile(r"\s*(?:[;:—–]|\s-\s|,\s*(?:and|but|so)\s|\s(?:and|but)\s(?=i\b|i'll|we\b|then\b))\s*", re.I)
_HOST_PAYOFF = re.compile(
    r"\b(?:and|then|so(?:\s+that)?)\s+(?:i|we)(?:'ll|\s+will|\s+can|\s+could)\b|\bfor\s+me\s+to\b"
    r"|\bto\s+(?:continue|proceed|go\s+on|start|carry\s+on)\b|\bbefore\s+(?:i|we)\b|\bafter\s+that,?\s+i\b",
    re.I)
_COUNTERFACTUAL = re.compile(r"\b(?:would|could|might|should|must)\s+have\b|\b(?:i|we|you|it)'d\s+have\b"
                             r"|\bhad\s+(?:i|you|we)\b", re.I)

# The one-word replies a go-ahead is asked for with (any language).
_ASSENT_WORDS = {"go", "yes", "y", "ok", "okay", "build", "proceed", "continue",
                 "sí", "si", "ja", "oui", "да", "是", "好", "はい", "نعم"}
_EMOJI = r"[\U0001F300-\U0001FAFF☀-➿]"
_TOKEN = (r"(?:QTOK\d+|the\s+word|(?:a\s+)?thumbs[- ]?up|" + _EMOJI + r"+|(?:"
          + "|".join(sorted(map(re.escape, _ASSENT_WORDS), key=len, reverse=True)) + r")\b)")
_PAYOFF = (r"(?:\b(?:and|then)\s+i(?:'ll|\s+will)\b|\bfrom\s+you\b|\bto\s+(?:continue|proceed|go\s+on|start)\b"
           r"|\bund\s+ich\b|\bet\s+je\b|\be\s+io\b|\bи\s+я\b|\by\s+\w+)")

# --- (c) replies -------------------------------------------------------------
# Verbs whose object goes BACK TO THE HOST.
_REPLY_TO_HOST = re.compile(_OPENS + r"(?:reply|respond|answer|write\s+back|get\s+back\s+to\s+me)\b", re.I)
# Verbs that send a word; to the Host unless a third party is named.
_SEND_WORD = re.compile(_OPENS + r"(?P<v>say|type|enter|send|drop|post|give|hit\s+reply)\b(?P<rest>[^.!?]*)", re.I)
# A named recipient that is not the Host: "to whoever", "to the old layout".
_OTHER_RECIPIENT = re.compile(r"^\s*(?!(?:me|us)\b)[^.!?]*?\bto\s+(?!(?:me|us|continue|proceed|go|start|confirm)\b)"
                              r"(?:whoever|anyone|someone|your\s+\w+|the\s+\w+|(?-i:[A-Z][a-z]+\b))", re.I)
# The user's own action as the purpose: "Type `ls` to list the workspace."
_OWN_PURPOSE = re.compile(r"\bto\s+(?:see|list|view|check|open|run|inspect|print|show|read|verify|find|test"
                          r"|install|start\s+it|launch|build\s+it|compile|log\s+in|look|browse|search)\b", re.I)

# --- (b) requests, hand-overs, halts -----------------------------------------
_ASK_USER = re.compile(
    _OPENS + r"(?:let\s+me\s+know|lmk|tell\s+me|keep\s+me\s+posted|shout|holler|(?:ping|nudge|poke|message|text"
    r"|buzz)\s+me|give\s+me|send\s+me"
    r"|drop\s+me|advise|confirm|approve|review|check|pick|choose|select|decide|clarify|specify|share|provide|paste"
    r"|upload|attach|forward"
    r"|ask(?:\s+me)?|feel\s+free\s+to\s+(?:ask|tell|let\s+me\s+know|ping|reach\s+out|say|shout))\b", re.I)
_HAND_OVER = re.compile(
    r"\b(?:the\s+)?ball(?:'s|\s+is)?\s+in\s+your\s+court\b|\bin\s+your\s+court\b|\bin\s+your\s+hands\b"
    r"|\byour\s+(?:call|move|decision|choice|turn)\b|\bup\s+to\s+you\b|\bover\s+to\s+you\b"
    r"|\b(?:for|to)\s+you\s+to\s+(?:decide|choose|pick|say|call)\b|\byours\s+to\s+(?:decide|choose|call|make)\b"
    r"|\bleave\s+(?:the|this|that|it)?\s*(?:decision|choice|call)?\s*(?:up\s+)?to\s+you\b"
    r"|\b(?:decision|choice|call)\s+(?:is\s+)?(?:with|to)\s+you\b|\b(?:is|'s|are)\s+(?:now\s+)?on\s+you\b", re.I)
_YOUR_REPLY = re.compile(
    r"\byour\s+(?:yes|nod|thumbs[- ]?up|say(?:-so)?|feedback|thoughts|input|opinion|take|views?|review|approval|confirmation|ok|okay"
    r"|go-?\s?ahead|decision|answer|reply|response|say-so|sign-?off|green\s+light|word|signal|pick"
    r"|verdict|instructions?|preference|nod|blessing)\b", re.I)
_HALT = re.compile(
    r"\b(?:await(?:ing)?|stand(?:ing)?\s+by|hold(?:ing)?\s+off|on\s+hold"
    r"|(?:i|we)(?:'ll|\s+will|'m|\s+am|'re|\s+are)\s+(?:now\s+)?(?:pause|stop|wait|hold|pausing|stopping|waiting|holding)"
    r"|(?:pausing|stopping|waiting)\s+(?:here|for|until|on)|stop(?:ping)?\s+here)\b"
    r"|^(?:i'm\s+|i\s+am\s+|we're\s+)?ready\s+when(?:ever)?\s+you", re.I)
# The user handed an evaluation task: "for you to review / check / approve".
_FOR_YOU_TO = re.compile(r"\bfor\s+you\s+to\s+(?:review|check|approve|confirm|decide|choose|pick|select|look\s+over"
                         r"|go\s+over|sign\s+off|weigh\s+in|ok|okay|test|try)\b", re.I)
# The Host wanting the user's input: "I'd love / appreciate a look, your take …".
_WANTS_INPUT = re.compile(
    r"\b(?:i'd|i\s+would|we'd|we\s+would)\s+(?:really\s+)?(?:love|like|appreciate|welcome|value)\s+(?:a|an|your|some)?"
    r"\s*(?:[\w-]+\s+){0,4}(?:eyes|look|glance|review|feedback|thoughts|input|opinion|take|check|sign-?off|ok"
    r"|okay|approval|confirmation|go-?\s?ahead|answer|views?|reaction)\b", re.I)
# "Whenever you're ready." — a sentence that is only a wish condition.
_BARE_WISH = re.compile(r"^(?:whenever|when|if|once)\s+(?:you(?:'re|\s+are)\s+ready|you\s+(?:like|want|wish|please)"
                        r"|ready|helpful|useful)\W*$", re.I)

# --- conditions ----------------------------------------------------------------
# A condition makes a Host step WAIT on the user only when the user is its
# SUBJECT, performing or deciding it ("when you're ready", "once you confirm",
# "if you want"), or when it is the user's APPROVAL of the work ("if that works
# for you", "if it looks good", "if helpful", "once approved", "pending your
# nod"). A condition about the state of the world or of the result — its
# subject a thing, its predicate not an approval ("if this one dies the same
# way", "if git complains", "if it hangs", "if it looks different from what
# you expected") — is a CONTINGENCY: the Host says what it will do if the
# world goes a certain way, which asks nothing. So does the user OBSERVING the
# result ("if you don't see it", "if you hit an error"): that is the result's
# state, seen by them. A hypothetical about the Host ("if I could see your
# screen, I'd …") is counterfactual.
#
# THE ONE LEXICON: which predicates are an approval of the work. It is small
# and semantic — good, fine, ok, right, works, helps, useful, fits, makes
# sense, worth it, what you wanted … — and no grammar tells it apart from any
# other predicate, so it is a named list (_APPROVAL). Every other lexicon in
# this module is a closed grammatical class (auxiliaries, pronouns,
# subordinators, assent words, the signal nouns a reply is named by).
_COND = re.compile(
    r"\b(?:if|when|whenever|once|in\s+case|until|till|unless|without|before|after|as\s+soon\s+as"
    r"|provided(?:\s+that)?|assuming(?:\s+that)?|upon|given\s+that)\s+((?!to\b)[^,.;:!?—–]+)"
    r"|(?:^|[,;]\s*)(?:should|were|had)\s+((?:you|i|we|it|anything|something|the\s+\w+|this|that)\b[^,.;:!?]*)"
    r"|\b(?:with|on|at|pending|awaiting|upon)\s+((?:your|a|an|the)\s+(?:quick\s+)?" + r"(?:ok|okay|go-?\s?ahead"
    r"|approval|signal|word|say(?:-so)?|confirmation|nod|sign-?off|green\s+light|command|cue|thumbs[- ]?up"
    r"|blessing|yes)\b[^,.;:!?]*)", re.I)
# The nouns a user's reply or signal is named by.
_SIGNAL = (r"(?:say(?:-so)?|word|nod|thumbs[- ]?up|go-?\s?ahead|ok|okay|approval|blessing|sign-?off|green\s+light"
           r"|input|decision|choice|call|verdict|confirmation|answer|reply|response|feedback|thoughts|signal|cue"
           r"|command|instructions?|pick|preference|opinion|take|views?|review|yes)")
# Approval of the work: the one semantic lexicon (see above).
_APPROVAL = (r"(?:good|great|fine|ok|okay|alright|all\s+right|right|correct|acceptable|useful|helpful|worth\s+it"
             r"|worthwhile|better|preferable|works?|helps?|suits?|fits?|makes\s+sense|sounds?\s+(?:good|right|fine)"
             r"|appeals?|convenient|what\s+you\s+(?:want|wanted|need|needed|had\s+in\s+mind))")
_USER_SUBJECT = re.compile(r"^(?:that\s+)?(?:you|you're|you've|you'd|you'll|y'all)\b", re.I)
_CONTRACT = [(re.compile(r"n't\b", re.I), " not"), (re.compile(r"'s\b", re.I), " is"),
             (re.compile(r"'re\b", re.I), " are"), (re.compile(r"'m\b", re.I), " am"),
             (re.compile(r"\bcannot\b", re.I), "can not")]


def _expand(text: str) -> str:
    for rx, rep in _CONTRACT:
        text = rx.sub(rep, text)
    return text


# A clause right after a verb of telling is its OBJECT, an indirect question —
# "Tell me if the silk comes up now" asks the user to report — not a condition
# ("If it looks off, tell me").
_TELL_VERB = re.compile(r"\b(?:tell\s+me|let\s+me\s+know|lmk|shout|ping\s+me|say|ask(?:\s+me)?|check|see"
                        r"|confirm|find\s+out|know)\s*$", re.I)
# The second semantic lexicon, the mirror of _APPROVAL: a negative evaluation
# of the result ("looks off", "is missing", "still flickers"). It is read only
# in such an object clause, where it makes "tell me if anything looks off" a
# contingency rather than a request to report.
_DEVIATION = re.compile(r"\b(?:off|wrong|different|odd|weird|strange|broken|missing|garbled|flat|blank|empty"
                        r"|stale|glitch\w*|fail\w*|break\w*|broke|crash\w*|errors?|problems?|issues?|bugs?"
                        r"|still|not|never|nothing|no)\b", re.I)


# The user's volition, which a negation does not turn into a deviation
# ("tell me if you don't want it" still asks).
_USER_WILL = re.compile(r"^(?:that\s+)?you(?:'d|\s+would|\s+do|\s+did)?(?:\s+not)?\s+(?:want|like|need|prefer"
                        r"|mean|meant|think|rather|wish|care)\b", re.I)
# The user noticing an indefinite something — "if you spot any typos", "if you
# hit an error", "if you see anything odd": an incident, whatever the noun.
_NOTICES_SOMETHING = re.compile(
    r"^(?:that\s+)?you(?:\s+do|\s+ever|\s+still|\s+happen\s+to)?\s+(?:see|spot|find|notice|hit|get|run\s+into"
    r"|encounter|come\s+across)\s+(?:any|an?|anything|something)\b", re.I)
# The connectives that make a halt: the Host's state persists until the user.
_WAITING_ON = re.compile(r"\b(?:until|till|unless|without|pending|awaiting)\s+((?!to\b)[^,.;:!?—–]+)", re.I)


def _conditions(s: str) -> list[str]:
    """The sentence's conditions — not the object clauses of telling verbs."""
    return [c for _, c in _conds(s)]


def _conds(s: str) -> list[tuple[str, str]]:
    """(connective, clause) for each condition of the sentence."""
    out = []
    for m in _COND.finditer(s):
        if _TELL_VERB.search(s[:m.start()]):
            continue
        out.append((m.group(0), (m.group(1) or m.group(2) or m.group(3)).strip()))
    return out


def _object_clauses(s: str) -> list[str]:
    """The if-clauses that are the object of a verb of telling."""
    return [(m.group(1) or "").strip() for m in _COND.finditer(s)
            if m.group(1) and _TELL_VERB.search(s[:m.start()])]


# The user's verbs that are a decision, a wish or an evaluation of the work, or
# that supply the Host with something it needs.
_USER_DECIDES = re.compile(
    r"^(?:that\s+)?you(?:'d|'re|'ve|'ll|\s+would|\s+are|\s+have|\s+do|\s+did|\s+still|\s+also|\s+just)*(?:\s+not)?\s+"
    r"(?:want|like|prefer|need|wish|choos|chose|pick|decid|confirm|approv|say|said|repl|answer|tell|told|giv|gave"
    r"|agree|ok|okay|sign(?:ed)?\s+off|ready|happy|keen|fine|good|comfortable|interested|done|back|had\s+a\s+look"
    r"|look(?:ed)?\s+over|review|check|test|tri|try|read|weigh(?:ed)?\s+in|get\s+back|got\s+back|mean|meant|think"
    r"|thought|rather|care)\w*\b"
    # or supplies the Host: "if you send me the key", "if you paste the token here"
    r"|^(?:that\s+)?you(?:'d|'ll|\s+can|\s+could|\s+just)*\s+(?:send|give|gave|paste|upload|share|provide|attach|forward"
    r"|drop|post)\w*\b(?![^,]*\bto\s+(?!me\b|us\b)\w)", re.I)
_PASSIVE_APPROVAL = re.compile(r"\b(?:is|are|was|were|gets|got|has\s+been|have\s+been|been)\s+(?:approved|reviewed"
                               r"|signed\s+off|confirmed|accepted|ok'?d|okayed|green-?lit|cleared|agreed)\b", re.I)
# A report-back: the Host tells the user later, which waits on nothing.
_REPORT_BACK = re.compile(r"\b(?:i|we)(?:'ll|\s+will)\s+(?:report(?:\s+back)?|post|share|let\s+you\s+know"
                          r"|update\s+you|ping\s+you|tell\s+you|follow\s+up|circle\s+back|come\s+back"
                          r"|drop\s+(?:it|them|the\s+\w+)\s+here|send\s+(?:you\s+)?(?:the\s+)?(?:result|diff|link|numbers"
                          r"|update|summary))\b", re.I)


# THE USER AS THE EVALUATOR of a condition, whatever its subject: "on your
# end", "for you", "to your liking", "what you're after", "the way you
# wanted", "the name you picked". _APPROVAL is left only for bare evaluative
# conditions with no stated evaluator ("if it looks good").
_USER_EVALUATES = re.compile(
    r"\bon\s+your\s+(?:end|side)\b|\b(?:for|to)\s+you\b|\bto\s+your\s+(?:liking|taste|satisfaction|standards?)\b"
    r"|\bwhat\s+you(?:'re|\s+are|\s+were|'d|\s+had|\s+have)?\s+(?:after|looking\s+for|wanted|want|need(?:ed)?|meant"
    r"|expected|had\s+in\s+mind|asked\s+for|liked?)\b"
    r"|\bthe\s+way\s+you\b"
    r"|\b(?:the|a|an|that|this)\s+[\w-]+\s+you\s+(?:wanted|asked\s+for|meant|expected|had\s+in\s+mind|liked"
    r"|preferred|picked|chose|need(?:ed)?)\b", re.I)


def _cond_kind(cond: str, conn: str = "") -> str:
    """'user' (the user performs or decides it, or their signal), 'approval'
    (an approval of the work), 'counterfactual' (a hypothetical), or 'world'
    (a contingency on the state of the world or the result)."""
    c = cond.strip()
    e = _expand(c)
    if re.match(r"^(?:i|we)\s+(?:could|were|was|had|would)\b", e, re.I) \
            or re.search(r"\byou(?:'d|\s+had)\s+\w+ed\b", c, re.I):
        return "counterfactual"
    if re.match(r"^your\s+(?:quick\s+)?" + _SIGNAL + r"\b", c, re.I) \
            or re.match(r"^(?:a|an|the)?\s*(?:quick\s+)?" + _SIGNAL + r"(?:\s+from\s+you)?\s*$", c, re.I) \
            or re.search(r"\b" + _SIGNAL + r"\s+from\s+you\b", c, re.I) \
            or re.match(r"^i\s+hear\s+(?:from\s+you|back)\b", c, re.I) \
            or re.match(r"^(?:approved|confirmed|signed\s+off|green-?lit|ok'?d|agreed|accepted|so)\b", c, re.I):
        return "user"
    if _PASSIVE_APPROVAL.search(e):
        return "user"                                   # "once it's approved", "when it's been reviewed"
    if _USER_SUBJECT.match(c):
        tail = re.search(r"\b(?:and|but)\s+(?:it|this|that|they|something|anything|the\s+\w+)\b(.*)$", e, re.I)
        if tail and _DEVIATION.search(tail.group(1)):
            return "world"                              # "if you try it and it breaks": the result's problem
        # the user deciding, wanting or evaluating makes the Host wait; the user
        # acting on the result ("if you rename it") is a contingency for the Host
        if _USER_DECIDES.match(e) or re.match(r"^\W*(?:once|after|as\s+soon\s+as|when|until|till|unless|without"
                                               r"|before)\b", conn, re.I):
            return "user"                               # "once you're connected, I'll re-fire them"
        return "world"                                  # "if you rename it, I'll keep tracking it"
    if re.match(r"^(?:i|we)(?:'ve|\s+have)?\s+(?:have|get|got|hear|receive|see)\s+(?:back\s+)?"
                r"(?:your\b|(?:[\w-]+\s+){0,4}?from\s+you\b)", c, re.I):
        return "user"                                   # "once I have your list": the user's input
    if re.match(r"^(?:i|we)\b", c, re.I) or re.match(r"^(?!(?:any|some|every|no)thing\b)[a-z]+ing\b", c, re.I):
        return "host"                                   # "before I commit it", "before pushing": sequencing
    if _USER_EVALUATES.search(e):
        return "evaluated"                              # the user is the evaluator: "if it checks out on your end"
    negated = bool(re.search(r"\bnot\b", e, re.I))
    if re.match(r"^(?:helpful|useful|needed|wanted|desired|preferred|necessary|appropriate|convenient|ok|okay"
                r"|good|ready)\b", c, re.I):
        return "approval"
    if not negated and (
            re.match(r"^(?:(?:that|this|it|the\s+\w+|everything|all|these|those)\b[^,]*?\b)?" + _APPROVAL + r"\b",
                     e, re.I)                           # "that works for you", "if the name works"
            # any subject, as a hypothetical benefit: "if a README would help", "if more tests would be useful"
            or re.match(r"^(?:[\w'-]+\s+){0,6}?(?:would|could|might)\s+(?:be\s+)?(?:\w+\s+)?" + _APPROVAL + r"\b",
                        e, re.I)
            # a wish of anyone but the work itself: "if the team needs a copy" (not "if it needs more memory")
            or re.match(r"^(?!(?:it|this|that|they)\b)(?:[\w'-]+\s+){1,4}?(?:needs?|wants?|would\s+like|prefers?)\b",
                        e, re.I)):
        return "approval"
    return "world"


def _on_user(cond: str, conn: str = "") -> bool:
    return _cond_kind(cond, conn) in ("user", "approval", "evaluated")


# The connectives of a CONTINGENCY — a step that happens upon a state of the
# world. "before / until / without / unless" set a deadline or a halt instead:
# "I'd love a look before it goes live" still asks.
_UPON = re.compile(r"^(?:if|when|whenever|once|in\s+case|as\s+soon\s+as|after|provided|assuming|upon|given"
                   r"|should|were|had)\b", re.I)


def _contingent(s: str) -> bool:
    """The sentence is governed by conditions on the world (or hypotheticals)
    only — none the user's decision or approval, none the Host's own next step,
    each one a contingency ("if / when / once …", not "before / until …"): it
    asks nothing."""
    found = _conds(s)
    # A condition the user evaluates is a wait only with a Host step after it:
    # "if it looks different from what you expected, tell me" stays a contingency.
    host_step = bool(_HOST_ACTS.search(_COND.sub(" ", s)))
    kinds = [_cond_kind(c, whole) for whole, c in found]
    kinds = ["world" if k == "evaluated" and not host_step else k for k in kinds]
    return bool(found) and all(k in ("world", "counterfactual") for k in kinds) \
        and all(_UPON.match(re.sub(r"^\W+", "", whole)) for whole, _ in found)


# --- (a) offers ------------------------------------------------------------------
_OFFER = re.compile(
    r"(?P<head>\b(?:i|we)\s+(?:can|could)|\b(?:i|we)(?:'m|'re|\s+am|\s+are)\s+(?:also\s+|always\s+)?"
    r"(?:able|ready|happy|glad|willing|keen)\s+to|\b(?:i|we)(?:'d|\s+would)\s+(?:also\s+)?(?:be\s+)?"
    r"(?:(?:more\s+than\s+)?(?:happy|glad|willing|able|keen)\s+to|love\s+to)"
    r"|(?:^|[,;:—–]\s*)(?:(?:more\s+than\s+|always\s+)?(?:happy|glad|willing|keen)\s+to|can\s+also|could\s+also"
    r"|could\b(?!\s+(?:you|i|we|it|this|that|they|be)\b)|can\b(?!\s+(?:you|i|we|it|this|that|they|be)\b))"
    r"|\blet's\b(?!\s+(?:say|see|be\s+clear|recap))|\blet\s+us\b)(?P<rest>[^.!?]*)", re.I)
_OFFER_ADVERBS = re.compile(r"^\s*(?:(?:also|now|then|still|easily|quickly|next|just|definitely|certainly"
                            r"|gladly|go\s+ahead\s+and|straight\s+away)\s+)*", re.I)
# Verbs that REPORT, not work proposed ("I can see it", "happy to report").
_REPORT_VERBS = {"see", "hear", "confirm", "verify", "read", "find", "tell", "check", "know", "spot",
                 "notice", "say", "assure", "attest", "guarantee", "think", "report", "announce", "note"}
_BARE_HELP = re.compile(r"^help(?:\s+(?:anytime|any\s+time|further|more|out|again|with\s+(?:anything|that|more)))?"
                        r"\W*$", re.I)
_IF_WANT_ME = re.compile(r"\b(?:if|whenever|once|in\s+case)\s+you(?:'d|\s+would)?\s+(?:want|like|prefer|need)"
                         r"\s+(?:me|us)\s+to\b", re.I)
# The Host acting, or not acting, in a clause.
_HOST_ACTS = re.compile(r"\b(?:i|we)(?:'ll|\s+will|'d|\s+would|\s+can|\s+could|'m|\s+am|\s+won't|\s+will\s+not"
                        r"|'m\s+not|\s+am\s+not|\s+do\s+not|\s+don't|\s+shall)\b|\bnothing\s+(?:more\s+|else\s+)?"
                        r"(?:happens|will\s+happen|moves|changes|goes)\b|\bnothing\s+(?:more\s+|else\s+)?from\s+me\b"
                        r"|\b(?:it|this|that|they|the\s+\w+)(?:'ll|\s+will|\s+gets?)\s+(?:be\s+)?\w+(?:ed|en)\b"
                        r"|\bit\s+(?:stays|waits|sits)\b", re.I)
# Abstention: the Host not acting on the work ("I'll leave it as is").
_ABSTAIN = re.compile(r"\b(?:leave\s+(?:it|this|that|the\s+\w+)\s+(?:as\s+is|alone|there|untouched)"
                      r"|keep\s+(?:it|this)\s+as\s+is|not\s+touch|won't\s+touch|stay\s+out)\b", re.I)
_USER_ACTS = re.compile(r"\byoursel(?:f|ves)\b|\bon\s+your\s+own\b|\bby\s+hand\b", re.I)

# --- exits -------------------------------------------------------------------------
# A NEW request that names no work.
_NEW_REQUEST = re.compile(
    r"\b(?:anything|something)(?:\s+(?:else|more))?(?=\s+(?:i\s+can|comes\s+up|to\s+ask|,|\.|!|$)|\W*$)"
    r"|\bany\s+(?:other\s+|more\s+|further\s+)?questions?\b|\b(?:have|got)\s+(?:any\s+)?questions?\b"
    r"|\bif\s+you\s+need\s+(?:me|anything|help)\b|\banything\s+(?:else\s+)?comes\s+up\b", re.I)
_LATER = re.compile(r"\b(?:next\s+time|in\s+(?:the\s+)?future|another\s+time|how\s+it\s+(?:goes|went)"
                    r"|how\s+you\s+get\s+on|later)\b", re.I)
# Another program's prompt, NAMED.
_OTHER_PROMPT = re.compile(
    r"\b(?:when|if|once|whenever|after)\s+(?!(?:i|we|you|it|this|that|they|he|she)\b)"
    r"(?:the\s+|a\s+|an\s+|its\s+)?[\w.-]+(?:\s+[\w.-]+)?\s+(?:prompts?|asks?|requests?|shows?|displays?"
    r"|pops?\s+up|appears?)\b"
    r"|\b(?:at|in|into|to)\s+(?:the|that|its|a|each|any)\s+(?:\w+\s+)?(?:prompt|dialog|dialogue|terminal"
    r"|console|shell|installer|wizard|pop-?up)s?\b(?!\s+(?:below|here|above))", re.I)
# A real PLACE in the product, or a path.
_PLACE = (r"(?:(?:the|your|its|this|that)\s+)?(?:[\w-]+\s+){0,2}(?:tab|app|pane|panel|sidebar|tree|folder"
          r"|directory|dir|workspace|root|shelf|window|page|drawer|inbox|tickets|workbench|editor|repo"
          r"|repository|project|preview|desktop|view)\b|QTOK\d+|[\w.-]*/[\w./-]+")
_POINTER = re.compile(
    r"^(?:just\s+|please\s+|you\s+can\s+|you'll\s+)?(?:open|see|review|find|check|view|look\s+at|inspect|browse"
    r"|give\b[^.!?]*\ba\s+(?:look|glance|read))\b[^.!?]*?\b(?:in|at|from|under|on|via|inside)\s+" + _PLACE, re.I)
_REPLY_ASKED = re.compile(
    r"\b(?:confirm|approve|let\s+me\s+know|tell\s+me|reply|respond|answer|decide|advise)\b", re.I)
# A REAL approval surface for a real gate (docs/wiki/pages/approvals.md).
_REAL_GATE = re.compile(r"\b(?:phase|code\s+approval|merge|pull\s+request|(?:network|allow|outbound|egress)\s+rule"
                        r"|rule|machine\s+change|change\s+on\s+(?:your|the)\s+machine|folder\s+grant|grant|op|run"
                        r"|key\s+reveal|backup)\b", re.I)
_REAL_SURFACE = re.compile(r"\b(?:workbench|network\s+app|approval\s+device|(?:machine'?s\s+)?console"
                           r"|approval\s+card|card\s+over\s+the\s+chat|authenticator)\b", re.I)
_SOCIAL = re.compile(
    r"\bhow(?:'s|\s+is|\s+was|\s+are|\s+were)\s+(?:(?:your|the)\s+(?:day|morning|afternoon|evening|night|week|weekend"
    r"|trip|holiday|vacation|coffee|lunch|family)|you(?:\s+(?:doing|feeling|holding\s+up))?|things|life"
    r"|everything)\b(?:\s+going)?"
    r"|\b(?:did|do)\s+you\s+(?:have|enjoy)\s+(?:a\s+good|a\s+nice|your|the)\s+(?:day|morning|weekend|evening"
    r"|trip|break|holiday)\b"
    r"|^\W*(?:(?:and\s+)?(?:how|what)\s+about\s+you|and\s+you)\b"
    r"|\bwhat(?:'s|\s+is)\s+on\s+your\s+mind\b"
    r"|^\W*(?:are|were)\s+you\s+(?:doing\s+)?(?:okay|ok|alright|all\s+right|well|good|feeling\s+\w+|holding\s+up)"
    r"\b(?!\s+(?:with|to|if|about|for))"
    r"|^\W*(?:busy|good|nice|long|quiet|rough|great|big)\s+(?:day|morning|week|weekend|night|trip|one)\W*$", re.I)

# --- drafts --------------------------------------------------------------------------
_DRAFT_LEAD = re.compile(r"\b(?:draft|email|e-mail|message|note|post|tweet|text|reply|letter|announcement"
                         r"|caption|comment|dm|memo|script|wording|copy)\b[^.!?]*:\s*$", re.I)
_SALUTATION = re.compile(r"^(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening)|greetings)\b[^,\n]{0,40},",
                         re.I)
_RECIPIENT = re.compile(r"\b(?:to|for|send|email|message|text)\s+(?:(?-i:[A-Z][a-z]+)\b|the\s+(?:team|group|board|client|customer"
                        r"|landlord|vendor|channel|list)\b|your\s+(?:team|boss|manager|client|landlord|colleagues?)\b"
                        r"|everyone\b|all\s+of\s+them\b)", re.I)
_SIGNOFF = re.compile(r"^(?:best|thanks|thank\s+you|cheers|regards|kind\s+regards|warmly|sincerely|love"
                      r"|talk\s+soon|—|-{1,2})\b", re.I)

# --- (d) questions ------------------------------------------------------------------
_OPENERS = re.compile(r"^(?:yes|no|yep|yeah|nope|nah|sure|of\s+course|certainly|absolutely|definitely|indeed"
                      r"|correct|not\s+really|not\s+at\s+all)\b", re.I)
_FACT_OPENERS = re.compile(r"^(?:yes|no|yep|yeah|nope|nah|correct|indeed|not\s+really|not\s+at\s+all)\b", re.I)
_WH_FACT = {"what", "where", "when", "why", "how", "who", "whose", "whom"}
_AUX = (r"(?:is|are|am|was|were|do|does|did|has|have|had|can|could|will|would|should"
        r"|shall|may|might|must)")
_DET = r"(?:the|this|that|these|those|its|our|their|my)"
_SUBJ = (r"(?:" + _DET + r"\s+[a-z][\w-]*|i|we|you|it|this|that|these|those|they|he|she|there|anything|something"
         r"|everything|nothing)")
_Q_SUBJECT = re.compile(
    r"^(?:(?:what|where|when|why|how|which|who|whose)\b[^?]*?\s)?(?P<aux>" + _AUX
    + r")(?:n't)?\s+(?P<subj>" + _SUBJ + r")\b", re.I)
_A_SUBJECT = re.compile(r"^(?:and|but|so)?\s*(" + _SUBJ + r")(?:('s|'re|'m|'ve|'d|'ll)|\s+([a-z']+))", re.I)
_MODAL = re.compile(r"^(?:'d|'ll|would|could|will|can|might|may|should|shall|must|ought)$", re.I)
_EVALUATIVE = re.compile(
    r"\b(?:ok|okay|alright|all\s+right|fine|good|great|right|correct|acceptable|work|works|look|looks|sound"
    r"|sounds|make\s+sense|makes\s+sense|suit|suits|fit|fits|help|helps|enough|clear|better|worse|preferable"
    r"|useful|helpful|what\s+you\s+(?:wanted|meant|expected|had\s+in\s+mind|asked\s+for))\b", re.I)
_PAST_AUX = {"did", "was", "were", "had"}
_CLAUSE_SPLIT = re.compile(r"\s+[—–-]\s+|[,;:]\s+")
_REPLY_VERBS_Q = r"(?:confirm|approve|tell|let|send|reply|check|review|decide|choose|pick|give|share|provide|advise|clarify)"

# --- segmentation ---------------------------------------------------------------------
_FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1")
_QUOTED = re.compile(r'"([^"\n]+)"|(?<![\w])\'([^\'\n]{1,40})\'(?![\w])')
_LIST_ITEM = re.compile(r"^(?:[-+*•]|\d+[.)])\s+(?:\[[ xX]\]\s*)?")
_STANDALONE = re.compile(r"^(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1$")
_QA_LABEL = re.compile(r"^(?:q|question|a|answer)\s*[:.]\s+", re.I)
_EMPHASIS = re.compile(r"[*~]+|(?<!\w)_+|_+(?!\w)")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
# An abbreviation's period is never a sentence end ("vs. OpenAI-specific
# calls?", "(e.g. Plotly)", "cf.", "approx."), whatever case the next word has.
# Only "etc." can end a sentence: before a capitalised word at a real sentence
# start, outside parentheses.
_ABBREV_END = re.compile(r"(?:^|[\s(\[])(?:vs|e\.g|i\.e|eg|ie|approx|cf|incl|esp|al|ca|resp|w\.r\.t)\.[\"')\]]*$", re.I)
_ETC_END = re.compile(r"(?:^|[\s(\[,])etc\.[\"')\]]*$", re.I)


def _sentences(text: str) -> list[str]:
    pieces = _SENTENCE_END.split(text)
    out: list[str] = []
    for p in pieces:
        if out and (_ABBREV_END.search(out[-1])
                    or (_ETC_END.search(out[-1]) and (not re.match(r"^[A-Z]", p)
                                                      or out[-1].count("(") > out[-1].count(")")))):
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out
_QUESTION = re.compile(r"\?[^\w]*$")
_QTOK = re.compile(r"QTOK(\d+)")
_IDENT = re.compile(r"\b(?!QTOK\d)(?!(?:e\.g|i\.e|w\.r\.t|a\.k\.a)\b)[A-Za-z][\w-]*(?:[._/][\w-]+)+\b", re.I)
_ASK_OPEN = re.compile(r"^(?:do\s+you\s+want|would\s+you\s+like|want\s+(?:me|us)\s+to|want\s+to|need\s+me\s+to"
                       r"|should\s+(?:i|we)|shall\s+(?:i|we)|can\s+i|may\s+i)\b", re.I)
_LEAD_OFFER = re.compile(r"\b(?:next\s+steps?|follow[- ]?ups?|suggestions?|ideas|options)\b", re.I)
_REPORTED = re.compile(r"\b\w+ed\b|\b(?:used|done|ran|made|found|built|kept|seen|taken)\b", re.I)

# What a rule returns when it fires: the words it fired on, and what the
# reading rests on — None for an explicit form (a "?" to the user, "say go",
# "want me to", "tell me"), else the judgement it took (a condition's subject,
# the approval lexicon, an ability read as an offer). The verdict prints both,
# so a false FAIL shows itself in one read.
def _hit(trigger: str, basis: str | None = None) -> tuple[str, str | None]:
    return (re.sub(r"\s+", " ", trigger).strip()[:120], basis)


class Fired(str):
    """A rule label (compares equal to L_*) that also carries `why`, the words
    the rule fired on, and `sure`, how firm the reading is."""
    why: str = ""
    sure: str = ""
    basis: str | None = None
    level: str = "judged"
    core: bool = False
    explicit: bool = False

    def __new__(cls, label: str, hit: tuple[str, str | None] | None = None, core: bool = False):
        o = super().__new__(cls, label)
        trigger, basis = hit or ("", None)
        o.why, o.basis = trigger, basis
        # core ⊂ explicit ⊂ any: a core form is explicit whatever its basis said
        o.level = "core" if core else ("explicit" if basis is None else "judged")
        o.core, o.explicit = o.level == "core", o.level in ("core", "explicit")
        o.sure = ("high confidence: a named pattern" if o.core
                  else "high confidence: an explicit form" if o.explicit
                  else f"medium confidence: {basis}")
        return o


L_OFFER = "offers work instead of doing it (a)"
L_WAIT = "makes a Host step wait on the user (b)"
L_WORD = "asks for a reply (c)"
L_QUESTION = "asks a question the user must answer (d)"
L_LEAD = "a list lead-in that offers or asks (e)"
RULES: list[tuple[str, str]] = [
    (L_WORD, "reply / respond / answer, or a word to say / type / send, however quoted"),
    (L_OFFER, "a first-person, 'we' or subject-elided capability or offer"),
    (L_WAIT, "a request to reply or decide, the decision handed over, the user's reply wanted, "
             "the Host halting, or a Host step conditioned on the user"),
    (L_LEAD, "a lead-in that offers or asks, read with its items"),
    (L_QUESTION, "a question the user must answer"),
]
CLOSING_PARAGRAPHS = 2


def _mask(text: str, quotes: list[str]) -> str:
    """Code spans and quotations become QTOK<n>; their text goes to `quotes`.
    A sentence end inside the quotation still ends the sentence (a quoted
    '?' ends it too, but is not the Host's question)."""
    def put(content: str) -> str:
        quotes.append(content.strip())
        return f"QTOK{len(quotes) - 1}" + ("." if content.rstrip().endswith((".", "!", "?")) else "")
    text = _INLINE_CODE.sub(lambda m: put(m.group(2)), text)
    text = _QUOTED.sub(lambda m: put(m.group(1) if m.group(1) is not None else m.group(2)), text)
    # an identifier ("review.adopt", "workspace_file", "config.yaml") is code, not a word
    return _IDENT.sub(lambda m: put(m.group(0)), text)


def _blocks(text: str) -> list[dict]:
    """[{"kind": "prose"|"code"|"rule"|"draft", "lines": [...]}]. Fences first
    (an unclosed fence runs to the end), then indented code and tables, then
    drafts: the paragraphs after a lead-in naming a draft, up to its sign-off
    or, with none, the one paragraph after it."""
    lines = text.split("\n")
    blocks: list[dict] = []
    cur: list[str] = []

    def flush():
        if cur:
            blocks.append({"kind": "prose", "lines": cur[:]})
            cur.clear()
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if m:
            flush()
            close = re.compile(r"^[ \t]*" + re.escape(m.group(1)[0]) + "{" + str(len(m.group(1))) + r",}\s*$")
            j = i + 1
            while j < len(lines) and not close.match(lines[j]):
                j += 1
            blocks.append({"kind": "code", "lines": lines[i:j + 1]})
            i = j + 1
            continue
        if not lines[i].strip():
            flush()
        else:
            cur.append(lines[i])
        i += 1
    flush()
    for b in blocks:
        if b["kind"] != "prose":
            continue
        ls = [ln for ln in b["lines"] if ln.strip()]
        if all(ln.startswith(("    ", "\t")) for ln in ls) or all(ln.strip().startswith("|") for ln in ls):
            b["kind"] = "code"
        elif not any(re.search(r"\w", ln) for ln in ls):
            b["kind"] = "rule"
    # drafts: a message to someone else, shown to the user. It needs the FORM of
    # one — a salutation, a sign-off, or a recipient named in the lead-in
    # ("email to Dana:"); a lead-in word alone (note, message, reply …) is never
    # enough. It runs through its sign-off; with none, it is only the message's
    # first paragraph, and never the reply's last paragraph.
    prose_idx = [t for t, bb in enumerate(blocks) if bb["kind"] == "prose"]
    last_prose = prose_idx[-1] if prose_idx else -1
    for k, b in enumerate(blocks):
        if b["kind"] != "prose" or not _DRAFT_LEAD.search(b["lines"][-1].strip()):
            continue
        j = k + 1
        if j >= len(blocks) or blocks[j]["kind"] != "prose":
            continue                                    # marked (code) or nothing: nothing to type
        lead = b["lines"][-1]
        if re.search(r"\b(?:to|for)\s+you\b", lead, re.I):
            continue                                    # "my note to you": the Host's own words to the user
        if all(ln.strip().startswith(">") for ln in blocks[j]["lines"] if ln.strip()):
            blocks[j]["kind"] = "draft"                 # the message, quoted
            continue
        salutation = bool(_SALUTATION.match(blocks[j]["lines"][0].strip()))
        recipient = bool(_RECIPIENT.search(lead))
        signoff = next((t for t in range(j, min(j + 8, len(blocks)))
                        if blocks[t]["kind"] == "prose"
                        and any(_SIGNOFF.match(ln.strip()) for ln in blocks[t]["lines"])), None)
        if not (salutation or recipient or signoff is not None):
            continue
        inline_signoff = bool(re.search(r"(?:^|[.!?]\s+)(?:thanks|thank\s+you|cheers|best|regards)\b[^.!?]*[.!]?\s*$",
                                        " ".join(blocks[j]["lines"]), re.I))
        if signoff is not None:
            end = signoff
        elif j != last_prose or (salutation and (inline_signoff or recipient)):
            end = j                                     # a message closing the reply needs its own form
        else:
            end = j - 1
        for t in range(j, end + 1):
            if blocks[t]["kind"] == "prose":
                blocks[t]["kind"] = "draft"
    return blocks


def _prose_blocks(text: str) -> list[dict]:
    """The Host's own prose blocks, before a trailing run of code and the
    captions that introduce it (a one-sentence paragraph ending in ':' that
    itself asks nothing)."""
    t = (text or "").replace("\r\n", "\n").translate(_FOLD)
    blocks = [b for b in _blocks(t) if b["kind"] not in ("rule",)]
    end = len(blocks)
    while end > 0:
        b = blocks[end - 1]
        if b["kind"] in ("code", "draft"):
            end -= 1
            continue
        body = " ".join(ln.strip() for ln in b["lines"])
        if (end < len(blocks) and blocks[end]["kind"] == "code" and body.endswith(":")
                and len(_sentences(body)) == 1 and not _caption_asks(body)):
            end -= 1
            continue
        break
    return [b for b in blocks[:end] if b["kind"] == "prose"]


def _caption_asks(body: str) -> bool:
    quotes: list[str] = []
    s = _EMPHASIS.sub("", _mask(body, quotes)).rstrip(":").strip()
    return bool(_flag(s, quotes) or _ASK_OPEN.match(s))


def _units(block: dict) -> list[dict]:
    units: list[dict] = []
    for raw in block["lines"]:
        line = raw.strip()
        if not line:
            continue
        quote = line.startswith(">")
        line = re.sub(r"^(?:>\s*)+", "", line)
        item = bool(_LIST_ITEM.match(line))
        heading = line.startswith("#")
        line = _LIST_ITEM.sub("", line).lstrip("#").strip()
        standalone = heading or bool(_STANDALONE.match(line)) or bool(_QA_LABEL.match(line))
        quotes: list[str] = []
        line = _EMPHASIS.sub("", _QA_LABEL.sub("", _mask(line, quotes))).strip()
        if not line:
            continue
        if (units and not item and not heading and units[-1]["quote"] == quote
                and line[:1].islower()):
            u = units[-1]
            off = len(u["quotes"])
            u["text"] += " " + _QTOK.sub(lambda m: f"QTOK{int(m.group(1)) + off}", line)
            u["quotes"] += quotes
        else:
            units.append({"text": line, "quote": quote, "item": item, "standalone": standalone,
                          "heading": heading or bool(_STANDALONE.match(raw.strip().lstrip("#").strip())),
                          "quotes": quotes})
    for i, u in enumerate(units):
        u["sentences"] = [s.strip() for s in _sentences(u["text"]) if re.search(r"\w", s)]
        u["lead_items"] = []
        if u["text"].endswith(":") and not u["item"]:
            j = i + 1
            while j < len(units) and units[j]["item"]:
                u["lead_items"].append(units[j])
                j += 1
    return [u for u in units if u["sentences"]]


def closing_units(text: str) -> list[dict]:
    """The closing part (the last two prose paragraphs), as units — what the
    verdict quotes as how the reply ends."""
    return [u for b in _prose_blocks(text)[-CLOSING_PARAGRAPHS:] for u in _units(b)]


def _unmask(u: dict, text: str | None = None) -> str:
    """The unit's text with its quotations and code spans put back — nested
    ones too (a code span inside a quotation was masked first)."""
    out = u["text"] if text is None else text
    for _ in range(4):
        back = _QTOK.sub(lambda m: '"%s"' % u["quotes"][int(m.group(1))]
                         if int(m.group(1)) < len(u["quotes"]) else m.group(0), out)
        if back == out:
            break
        out = back
    return out


def closing_part(text: str) -> str:
    return "\n".join(_unmask(u) for u in closing_units(text))


# --- statements ------------------------------------------------------------------------
def _clause_at(s: str, pos: int) -> str:
    """The clause of `s` around position `pos`."""
    starts = [m.end() for m in _CLAUSE_BREAK.finditer(s) if m.end() <= pos]
    ends = [m.start() for m in _CLAUSE_BREAK.finditer(s) if m.start() > pos]
    return s[(starts[-1] if starts else 0):(ends[0] if ends else len(s))]


def _assent_token(tok: str, quotes: list[str]) -> bool:
    m = _QTOK.fullmatch(tok)
    word = quotes[int(m.group(1))] if m and int(m.group(1)) < len(quotes) else tok
    return word.strip().lower().strip("!.") in _ASSENT_WORDS or bool(re.fullmatch(_EMOJI + "+", word.strip()))


# The Host's own next step as a payoff ("so I can continue", "and I'll push it").
_HOST_STEP = re.compile(r"\b(?:and|then|so(?:\s+that)?)\s+(?:i|we)(?:'ll|\s+will|\s+can|\s+could)\b"
                        r"|\bfor\s+me\s+to\b|\bbefore\s+(?:i|we)\b", re.I)
_SUBORDINATE = re.compile(r"^\s*(?:if|when|whenever|once|after|before|until|as\s+soon\s+as|should|otherwise|also"
                          r"|alternatively|either\s+way|meanwhile|in\s+the\s+meantime|for\s+now|until\s+then"
                          r"|in\s+that\s+case|if\s+so|if\s+not)\b", re.I)
# The user told to act, with the Host's next step as the payoff ("give it a
# read and I'll tweak it", "click Approve so I can continue"). An imperative
# is recognised by POSITIVE evidence only: its first word is followed directly
# by that verb's object (a pronoun, a determiner, or a capitalised control
# name), and the word is not of a closed class that opens a statement (a
# pronoun, determiner, preposition, conjunction or adverb) nor a past, -ing,
# -ly or third-person form. "Phase 4 is running and I'll …", "File's written
# and I'll …" and "Codex finished the build, and I'll …" carry no such
# evidence, so they are statements.
_PAYOFF_STEP = re.compile(r"\b(?:and|then|so(?:\s+that)?)\s+(?:(?:whichever|whatever|wherever|however)\b[^,.;:!?]*?\s)?"
                          r"(?:i|we)(?:'ll|\s+will|\s+can|\s+could)\b", re.I)
_VERB_OBJECT = re.compile(r"^\W*([a-z]+)\s+(?:(?:it|them|me|us|this|that|these|those|the|a|an|your|my|our|its|their"
                          r"|some|any|all|both|each|every)\b|(?-i:[A-Z])[\w-]*\b)", re.I)
_STATEMENT_OPENERS = {
    "i", "we", "you", "he", "she", "it", "they", "this", "that", "these", "those", "there", "here", "the", "a", "an",
    "my", "our", "your", "its", "their", "each", "every", "all", "both", "one", "some", "any", "no", "none",
    "nothing", "everything", "something", "anything", "everyone", "someone", "anyone", "nobody",
    "with", "without", "for", "from", "at", "in", "on", "by", "to", "of", "into", "onto", "about", "after",
    "before", "since", "until", "during", "through", "under", "over", "per", "via", "as", "like", "than",
    "and", "but", "or", "nor", "so", "yet", "because", "while", "though", "although", "if", "when", "once",
    "now", "then", "also", "still", "just", "only", "even", "meanwhile", "otherwise", "plus", "besides",
    "again", "already", "soon", "today", "tomorrow", "tonight", "yesterday", "next", "first", "last", "finally",
    "done", "ready", "good", "great", "ok", "okay", "yes", "sure", "thanks", "not", "never", "always"}
_IRREGULAR_PAST = {
    "wrote", "built", "bought", "caught", "broke", "drove", "fed", "felt", "fought", "forgot", "froze", "hid",
    "hung", "knew", "led", "lent", "lit", "lost", "meant", "met", "paid", "rode", "rang", "rose", "saw",
    "sought", "sold", "shook", "shot", "sang", "sat", "slept", "spent", "spun", "stood", "stole", "stuck",
    "struck", "swept", "taught", "tore", "thought", "threw", "understood", "woke", "wore", "wound", "dug",
    "fit", "flew", "grew", "drew", "blew", "bent", "bound", "dealt", "fled", "ground", "laid", "paid", "said"}


def _imperative_payoff(s: str) -> str | None:
    """The user told to act, and the Host's step after it: the imperative's
    words, or None."""
    for pm in _PAYOFF_STEP.finditer(s):
        head = re.split(r"[;:—–]|\s-\s", s[:pm.start()])[-1]
        if _SUBORDINATE.match(head) and "," in head:
            head = head.split(",", 1)[1]                # past "When the dialog appears,"
        m = _VERB_OBJECT.match(head)
        if not m:
            continue
        w = m.group(1).lower()
        if w in _STATEMENT_OPENERS or w in _IRREGULAR_PAST or _PAST_VERB.fullmatch(w) \
                or w.endswith(("ed", "ing", "ly")) or (w.endswith("s") and not w.endswith(("ss", "us"))):
            continue
        if _NEG.search(head) or _LATER.search(head):
            continue
        return head.strip()
    return None


def _scheduled(s: str) -> bool:
    """A condition on an EXPECTED event ("when the dialog appears", "once it
    lands") rather than a possible one ("if anything looks off"): a request to
    the user timed by it is part of the plan, and still asks."""
    return any(re.match(r"^\W*(?:when|once|as\s+soon\s+as|after|whenever)\b", m.group(0), re.I)
               for m in _COND.finditer(s) if m.group(1))




# A REPLY REQUEST: the user asked to tell the Host something, to supply what
# it needs, or to give it a signal. With a Host step after it ("let me know and I'll commit it", "say so
# and I'll redispatch") the Host's step waits on that reply, whatever the
# condition around it; without one ("if it looks off, tell me") it does not.
_REPLY_REQUEST = re.compile(
    _OPENS + r"(?P<v>let\s+me\s+know|lmk|tell\s+me|say\s+so|just\s+say|confirm|reply|respond|answer"
    r"|provide|paste|share|upload|attach|send\s+(?:me|us|over|it|them|that)|nod|hand\s+(?:me|us|it|them|that|over)"
    r"|pass\s+(?:me|us|it|them|along)|drop\s+me|give\s+me"
    r"|get\s+back\s+to\s+me|give\s+me\s+(?:a\s+|the\s+|your\s+)?(?:quick\s+)?(?:nod|word|go-?\s?ahead|ok|okay"
    r"|thumbs[- ]?up|shout|heads[- ]?up|green\s+light)|ping\s+me|holler|shout|drop\s+me\s+a\s+(?:line|note)"
    r"|(?:message|text)\s+me)\b", re.I)
# THE USER'S READINESS gating the Host's next step: "whenever you are", "when
# you're ready", "at your convenience". It gates only a step of the Host's: a
# Host step, the Host's own readiness, or a next step named.
_READINESS = re.compile(r"\b(?:when|whenever|once|as\s+soon\s+as|if)\s+you(?:'re|\s+are)"
                        r"(?:\s+(?:ready|set|good\s+to\s+go|free|around|back))?(?=\s*(?:[.,;:!?—–)]|$|\s+(?:to|for|and)\b))"
                        r"|\bwhen(?:ever)?\s+you\s+get\s+a\s+(?:chance|moment|minute|sec(?:ond)?)\b"
                        r"|\bat\s+your\s+(?:earliest\s+)?convenience\b|\bin\s+your\s+own\s+time\b", re.I)
_HOST_NEXT = re.compile(r"^\W*(?:(?:i'm|i\s+am|we're|we\s+are|all)\s+)?(?:ready|set|standing\s+by|here)\b"
                        r"|\bnext\s+(?:step|move|up|thing|phase|task|one|stage)\b"
                        r"|\b(?:i|we)(?:'ll|\s+will|'m|\s+am|'re|\s+are|\s+can|\s+could)\b", re.I)
# THE HOST NAMING INPUT IT NEEDS from the user: "One thing I need from you: …",
# "I still need your sign-off date".
_NEEDS_FROM_YOU = re.compile(
    r"\b(?:i|we)(?:'d|\s+would)?\s+(?:still\s+|just\s+|only\s+|also\s+|really\s+)?(?:need|require)\b[^.!?]{0,60}?"
    r"\bfrom\s+you\b"
    r"|\b(?:i|we)\s+(?:still\s+|just\s+|only\s+|also\s+)?(?:need|require)\s+your\s+(?!help\b)\w+", re.I)


# A DEFECT in the delivered result, stated by a condition ("if anything looks
# off", "if it doesn't show up", "should anything be broken"), makes the Host's
# step after a reply request a CONTINGENCY — the Host acts only if a defect
# exists — unless that step makes PROGRESS on the work (commit, push, ship …):
# then the reply is the go-ahead in disguise. A negated defect ("if nothing
# looks off", "if it isn't broken") is an approval, not a defect.
_DEFECT_WORD = re.compile(
    r"\b(?:off|wrong|incorrect|broken|breaks?|broke|missing|garbled|corrupt(?:ed)?|blank|empty|stale|glitch\w*"
    r"|fails?|failed|failing|errors?|crash\w*|bugs?|buggy|flicker\w*|odd|weird|strange|problems?|issues?"
    r"|misbehav\w*|needs?\s+fixing)\b", re.I)
_DEFECT_NEGATED_GOOD = re.compile(
    r"\b(?:not|never)\s+(?:\w+\s+){0,2}?(?:work\w*|right|correct|there|show\w*|load\w*|open\w*|match\w*|render\w*"
    r"|run\w*|compile\w*|appear\w*|land\w*)\b", re.I)
# Steps that MAKE PROGRESS on the work: the one list the exception reads.
_PROGRESS = re.compile(
    r"\b(?:commit|push|merge|ship|deploy|publish|release|launch|roll\s+(?:it\s+|them\s+)?out|proceed|continue"
    r"|carry\s+on|move\s+on|go\s+ahead|submit|finali[sz]e|hand\s+(?:\w+\s+)?(?:over|off|on)"
    # handing the work on to someone else: "send it to the team", "share it with Fern"
    r"|(?:send|share|forward)\s+(?:(?:it|them|this|that|the\s+[\w-]+)\s+)?(?:out|off|on|(?:over\s+)?(?:to|with)\s+(?!you\b|me\b))"
    r"|lock\s+(?:it\s+|them\s+|that\s+)?in|close\s+(?:it|this|that|them)\s+out|(?:start|begin|kick\s+off)\b(?:\s+\w+){0,3}?\s*"
    r"(?:next|phase|step|stage|on\b|work|build|the\s+\w+))", re.I)


def _states_defect(cond: str) -> bool:
    """The condition states a defect in the result, not its absence: a defect
    word with no negator before it ("if anything looks off"; not "if nothing
    looks off", "if it isn't broken"), or a good state negated ("if it
    doesn't show up", "if the content isn't right")."""
    e = _expand(cond)
    m = _DEFECT_WORD.search(e)
    if m:
        return not re.search(r"\b(?:no|not|nothing|never|none|without)\b", e[:m.start()], re.I)
    return bool(_DEFECT_NEGATED_GOOD.search(e))


def _defect_contingency(s: str, step_at: int) -> bool:
    """A condition in `s` states a defect in the result, and the Host's step
    from `step_at` does not make progress on the work."""
    if not any(_states_defect(m.group(0)) for m in _COND.finditer(s)):
        return False
    if re.match(r"^\W*before\s+(?:i|we)\b", s[step_at:], re.I):
        return False                                    # "tell me before I send it": the step is held on the user
    step = re.split(r"[.;:!?—–]", s[step_at:], maxsplit=1)[0]
    return not _PROGRESS.search(step)


def _reply_then_step(s: str) -> tuple | None:
    """A reply request with a Host step after it — unless its condition states
    a defect in the result and the step does not make progress on the work."""
    for m in _REPLY_REQUEST.finditer(s):
        v = m.start("v")
        if not _imperative_at(s, v) or _LATER.search(_clause_at(s, v)):
            continue
        if re.search(r"\b(?:don't|do\s+not|never|no\s+need\s+to)\s+(?:\w+\s+)?$", s[:v], re.I):
            continue                                    # "don't tell me …", "no need to let me know"
        step = _HOST_PAYOFF.search(s, m.end())
        if not step:
            continue
        if _NEW_REQUEST.search(s[m.end():step.start()]):
            continue                                    # "let me know if you need anything else and I'll …"
        if _defect_contingency(s, step.start()):
            continue                                    # "if anything looks off, tell me and I'll queue a pass"
        return _hit(s[v:step.end()])
    return None


def _bare_reply_request(s: str) -> bool:
    """A reply request made on its own ("just say", "let me know"), outside
    any condition: what turns an offer into the go-ahead it waits for."""
    spans = [c.span() for c in _COND.finditer(s)]
    for m in _REPLY_REQUEST.finditer(s):
        v = m.start("v")
        if any(a <= v < b for a, b in spans) or not _imperative_at(s, v) or _LATER.search(_clause_at(s, v)):
            continue
        if _NEW_REQUEST.search(s[m.end():]):
            continue
        return True
    return False


# A future Host commitment, and an item named generically (a description of
# how something works, not a step held on this item).
_FUTURE_HOST = re.compile(r"\b(?:i|we)(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to|\s+shall)\b", re.I)
_GENERIC_ITEM = re.compile(r"\b(?:one|any|each|every|anything|something)\b|\b(?:a|an)\s+(?!word\b|nod\b|go)\w+", re.I)


def _readiness_gate(s: str) -> tuple | None:
    """The Host's next step gated on the user being ready, or on their word
    ("Next up when you give the word: …")."""
    ready = _READINESS.search(s)
    m = ready or next((c for c in _COND.finditer(s)
                       if re.match(r"^\W*(?:when|whenever|once|as\s+soon\s+as)\b", c.group(0), re.I)
                       and _ON_REPLY.search(c.group(0))), None)
    if not m or _past_report(s, m.start()):
        return None
    if not ready and (not (_FUTURE_HOST.search(s.replace(m.group(0), " "))
                           or re.search(r"\bnext\s+(?:step|move|up|thing|phase|task|one|stage)\b", s, re.I)
                           or re.match(r"^\W*(?:(?:i'm|i\s+am|we're|we\s+are|all)\s+)?(?:ready|set|standing\s+by)\b", s, re.I))
                      or _GENERIC_ITEM.search(m.group(0))):
        return None                                     # "once you approve one, X lets me …": how it works
    if re.match(r"^\W*(?:you|feel\s+free)\b", s, re.I) or re.match(
            r"^\W*" + re.escape(m.group(0)) + r",?\s+you\b", s, re.I):
        return None                                     # "you can open it whenever you're ready": their own act
    if _HOST_NEXT.search(s.replace(m.group(0), " ")):
        return _hit(m.group(0))
    return None


def _needs_from_you(s: str) -> tuple | None:
    """The Host naming input it needs from the user, now."""
    for m in _NEEDS_FROM_YOU.finditer(s):
        clause = _clause_at(s, m.start())
        if _NEG.search(clause[:max(0, clause.find(m.group(0)))] + m.group(0)):
            continue                                    # "I don't need anything from you"
        if any(a <= m.start() < b for a, b in (c.span() for c in _COND.finditer(s))):
            continue                                    # "if I need anything from you, I'll ask"
        return _hit(m.group(0))
    return None


def _is_label(head: str) -> bool:
    """`head` (the text before a colon) is a label or lead clause opening its
    line: no earlier colon, short, and not addressed to the user."""
    head = re.split(r"(?<=[.!?])\s+", head)[-1].strip()
    return bool(head) and ":" not in head and len(head.split()) <= 10 \
        and not re.search(r"\b(?:you|your|yours)\b", head, re.I)


def _in_word_list(s: str, start: int) -> bool:
    """The word at `start` is one item of a list of single words ("insert,
    delete, comment and share"): two single-word items before it, or one
    before and one after. "Fix it, test it, and tell me" is not one."""
    segs = s[:start].split(",")
    last = segs[-1].strip().lower()
    if len(segs) < 2 and not re.fullmatch(r"[\w-]+\s+(?:and|or)", last):
        return False
    if last in ("", "and", "or"):
        n = 0
    elif re.fullmatch(r"[\w-]+\s+(?:and|or)", last):
        n = 1
    else:
        return False
    for seg in reversed(segs[:-1]):
        if not re.fullmatch(r"[\w-]+", seg.strip()):
            break
        n += 1
    nxt = bool(re.match(r"^[\w-]+(?:,\s+[\w-]+\b|\s+(?:and|or)\s+[\w-]+\s*(?:[.,;:!?)]|$))", s[start:]))
    return n >= 2 or (n >= 1 and nxt)


# AN ASK MENTIONED, NOT MADE: the request verb as the complement of a reported
# ask ("you asked me to write it", "why I kept asking you to say go", "I was
# telling you to …"). A present request ("I'm asking you to confirm") is made.
_REPORTED_ASK = re.compile(r"\b(?:(?:asked|told|requested|instructed|wanted)\s+(?:you|me|us)"
                           r"|(?:was|were|been|kept|keep|stop|stopped|quit|used\s+to\s+be)\s+(?:\w+\s+)?"
                           r"(?:asking|telling|requesting)\s+(?:you|me|us))\s+(?:to\s+)?$", re.I)


_CHAIN_AUX = re.compile(r"\b(?:should|would|could|can|will|must|might|may|shall|does|do|did|is|are|was|were|has|have"
                        r"|had|'ll|'d|'s|'re|'ve)\b", re.I)


def _chain_has_subject(before: str) -> bool:
    """The clause whose verb chain ends in `before` ("… and") has a subject
    other than the user: a finite auxiliary that is not its first word ("the
    bot should notice it and …", "it already reads the date and …"), or a
    subject word opening it. "You" as the subject, or no subject ("Open it
    and …", "Have a look and …"), is the user's chain."""
    head = re.split(r"[.!?;:—–(]|\s-\s", before)[-1]
    head = re.sub(r"^\s*(?:if|when|whenever|once|after|before|until|as\s+soon\s+as|should)\b[^,]*,", "", head, flags=re.I)
    head = re.sub(r"\s*(?:and|or|then)\s*$", "", head, flags=re.I).strip()
    words = head.split()
    if not words:
        return False
    first = words[0].lower().strip(",")
    if first in ("you", "you'll", "you're", "you've", "just", "please", "simply"):
        return False
    if first in _STATEMENT_OPENERS - {"just", "now", "then", "also", "first", "next", "please"} \
            or first.endswith("ly"):
        return True                                     # "The bot …", "It …", "Ideally the installer …"
    aux = _CHAIN_AUX.search(head)
    return bool(aux and aux.start() > 0)


def _imperative_at(s: str, start: int) -> bool:
    """A verb at `start` opens an imperative clause: the sentence start, a
    clause mark, or a comma closing a subordinate clause or a short lead
    ("If so, tell me", "Done, let me know") — not the next verb of a list
    ("… fetch your document, review it, and write …")."""
    if _REPORTED_ASK.search(s[:start]):
        return False                                    # "you asked me to …": reported, not requested
    before = s[:start].rstrip()
    before = re.sub(r"\b(?:just|simply|please|now|kindly)\s*$", "", before, flags=re.I).rstrip()
    if not before and re.match(r"^[\w-]+\s*:\s+\S", s[start:]):
        return False                                    # the word IS a label: "Review: not needed"
    sent = re.split(r"(?<=[.!?])\s+", before)[-1]
    if ":" in sent and _is_label(sent.split(":", 1)[0]):
        # STRUCTURE, not a request: the content after a label or lead at the
        # start of a line, to the sentence's end ("On commit: check for danger",
        # "On publish: lint, build, then upload", "The brief is fix-only: walk
        # every mode"). Still a request when aimed at the Host ("tell me", "send
        # me") or paid for with the Host's step.
        rest = s[start:]
        return bool(re.match(r"^(?:let\s+me\b|lmk\b|[\w-]+\s+(?:me|us)\b)", rest, re.I)
                    or _HOST_PAYOFF.search(_clause_at(s, start) + " " + rest[:120]))
    if _in_word_list(s, start):
        return False                                    # "sync, compose, reply, labels": a noun list
    if not before or before[-1] in ";:—–(" or before.endswith(" -"):
        return True
    if before.endswith(","):
        seg = re.split(r"[;:—–(]|\s-\s", re.sub(r"\([^()]*\)", "", before[:-1]))[-1]
        seg = re.split(r"(?<=[.!?])\s+", seg)[-1]
        return bool(_SUBORDINATE.match(seg)) or len(seg.split()) <= 2
    word = re.split(r"[\s,;:]+", before)[-1].lower()
    if word in ("and", "or", "then") or before.lower().endswith((" and", " or", " then")):
        # A COORDINATED VERB INHERITS ITS CHAIN'S SUBJECT: imperative only when
        # the chain has none (it opens on its verb) or the user is its subject.
        # "Open it and say go" asks; "the prompt should know … and say …" does not.
        return not _chain_has_subject(before)
    return word in ("so", "otherwise", "else")


def _asks_reply(s: str, quotes: list[str]) -> bool:
    """(c) The user is asked to reply, or to send a word."""
    for m in _REPLY_TO_HOST.finditer(s):
        verb_at = m.end() - len(re.search(r"(?:reply|respond|answer|write\s+back|get\s+back\s+to\s+me)$",
                                          m.group(0), re.I).group(0))
        if _imperative_at(s, verb_at) and not _LATER.search(_clause_at(s, m.end())):
            return _hit(m.group(0), None if _HOST_PAYOFF.search(s, m.end())
                        else "a request to reply with no Host step waiting on it")
    for m in _SEND_WORD.finditer(s):
        v, rest = m.group("v").lower(), m.group("rest")
        if not _imperative_at(s, m.start("v")):
            continue
        if _LATER.search(_clause_at(s, m.start("v"))):
            continue
        if v == "give" and not re.match(r"^\s+(?:me|us)\b", rest, re.I):
            continue                                    # "give it a look": not a reply
        if v in ("send", "give", "say") and _OTHER_RECIPIENT.match(rest):
            continue                                    # to someone else
        if v in ("type", "enter", "send", "post") and _OWN_PURPOSE.search(rest) and not _HOST_PAYOFF.search(rest):
            continue                                    # the user's own command
        if v in ("type", "enter", "post", "drop") and not re.search(_TOKEN, rest, re.I):
            continue                                    # nothing to reply with
        return _hit(m.group(0))
    # whatever the verb: <word> <assent word> + the Host's step as payoff
    for m in re.finditer(r"(?:^|[,;:—–]\s*)(?:just\s+|please\s+)?[^\W\d_]+\s+(" + _TOKEN + r")\s*,?\s*"
                         + _PAYOFF, s, re.I):
        if _assent_token(m.group(1), quotes):
            return _hit(m.group(0), "an assent word right before the Host's next step")
    # the word named as the price of the next step, no verb
    for m in re.finditer(r"(" + _TOKEN + r")[^.!?]*?" + _PAYOFF, s, re.I):
        if _QTOK.fullmatch(m.group(1)) and _assent_token(m.group(1), quotes):
            return _hit(m.group(0), "a quoted assent word named as the price of the Host's next step")
    return None


# A proposal FOR THE FUTURE: what turns a statement of ability ("I can …")
# into an offer. Without one, "I can write anywhere in your workspace, so it
# went to the root" describes a capability.
_FUTURE_WORK = re.compile(r"\b(?:also|too|as\s+well|next|then|additionally|instead|go\s+ahead|straight\s+away"
                          r"|right\s+away|whenever|for\s+you|in\s+addition|afterwards|after\s+that|later|now)\b", re.I)
# What makes it a DESCRIPTION: a result or reason after it, a contrast, a
# generality, a list of abilities.
_DESCRIBES = re.compile(r"\b(?:so|since|because|as)\b|,\s*and\s+(?:it|this|that|the|so)\b|:|\bbut\b"
                        r"|\b(?:anywhere|anything|everything|only|generally|always|by\s+default|in\s+general)\b"
                        r"|,\s*\w+\s[^,]*,\s*(?:and|or)\s", re.I)
# "Let's" as a discourse marker, not a proposal of work.
_LETS_DISCOURSE = re.compile(r"\blet's\s+(?:call\s+it|be\b|say|see|recap|summari[sz]e|note|start\s+with"
                             r"|look\s+at|take\s+stock|wrap\s+(?:it\s+)?up|review\s+what|go\s+over)", re.I)
_WILLING = re.compile(r"happy|glad|willing|keen|ready|love|let's|let\s+us", re.I)


def _offers(s: str) -> bool:
    """(a) The Host proposes work of its own for the future. Willingness
    (happy / glad / ready to, I'd love to, let's) always proposes; ability
    (I can / could / am able to) proposes only when marked for the future,
    tied to the user's wish or reply, and not describing what it can do."""
    if _IF_WANT_ME.search(s):
        return _hit(_IF_WANT_ME.search(s).group(0), "an offer: \"if you want me to\"")
    tied = any(_on_user(c, whole) for whole, c in _conds(s)) or bool(_OTHER_REPLY.search(s)) \
        or bool(re.search(r"\b(?:just\s+say|say\s+the\s+word)\b", s, re.I))
    for m in _OFFER.finditer(s):
        head, rest = m.group("head").lower(), m.group("rest")
        if re.match(r"^(?:'t|n't|\s+not\b|not\b)", rest):
            continue                                    # a negation
        if re.match(r"^\s+(?:have|'ve)\b", rest) or _COUNTERFACTUAL.search(m.group(0)):
            continue                                    # counterfactual
        before = s[:m.start()].lower()
        if re.search(r"\b(?:so|so\s+that|in\s+order\s+that|where|which|that|if|when)\s*$", before):
            continue                                    # a purpose, relative or conditional clause
        if "could" in head and re.search(r"\b(?:glad|happy|good|nice|great)\s+(?:that\s+)?(?:i\s+)?$", before):
            continue                                    # "glad I could help": past
        if _LETS_DISCOURSE.match(s[m.start("head"):]):
            continue                                    # "Let's call it done: …"
        bare = _OFFER_ADVERBS.sub("", rest)
        if re.match(r"^\s*only\b", bare):
            continue                                    # a constraint
        verb = (re.match(r"^\s*([a-z']+)", bare.lower()) or [None, ""])[1]
        if not verb:
            continue
        if verb in _REPORT_VERBS and not tied:
            continue                                    # reports what is there
        if _BARE_HELP.match(bare.strip()):
            continue                                    # availability, no work named
        if not _WILLING.search(head):
            marked = bool(_FUTURE_WORK.search(before[-40:] + " " + m.group("head") + " " + rest)) or tied
            if not marked or (_DESCRIBES.search(rest) and not tied):
                continue                                # a capability described, not offered
            return _hit(m.group(0)[:80], "an ability (can / could) read as an offer: "
                        + ("tied to the user's wish, approval or reply" if tied else "marked for the future"))
        return _hit(m.group(0)[:80], "an offer: willingness to do more work")
    return None


# A proposal in the governed clause: an impersonal modal ("a commit would be
# the natural follow-up", "could be added"), or "is an option / is next".
_PROPOSAL = re.compile(r"\b(?:would|could|might)\s+be\s+(?:a|an|the)?\s*(?:[\w-]+\s+){0,2}(?:next\s+step|step"
                       r"|follow-?up|option|move|idea|addition|improvement|thing\s+to\s+do)\b"
                       r"|\b(?:can|could|would|might)\s+be\s+\w+(?:ed|en)\b"
                       r"|\b(?:can|could|would|might)\s+(?:follow|come\s+next|go\s+next)\b"
                       r"|\b(?:is|'s|are)\s+(?:also\s+)?(?:an?\s+option|next)\b", re.I)


def _requests(s: str) -> int:
    """How many requests to the user the sentence makes (imperatives of telling,
    deciding or supplying)."""
    return sum(1 for m in _ASK_USER.finditer(s))


def _names_work(s: str) -> bool:
    """A wish in the sentence names work: "if you want a README", "if you'd
    like me to commit it" — not "something", "anything", "questions"."""
    return bool(re.search(r"\byou(?:'d|\s+would)?\s+(?:want|like|prefer|need)\s+(?:me\s+to\s+\w+|(?:a|an|the|some"
                          r"|more|any)?\s*(?!(?:anything|something|it|this|that|questions?|help|me)\b)[a-z]\w+)", s, re.I))


# The user's REPLY or signal as what a Host step waits on: "once you confirm",
# "until I hear from you", "on your word".
_ON_REPLY = re.compile(r"\b(?:you(?:'ve|\s+have)?\s+(?:say|said|tell|told|confirm|confirmed|approve|approved|ok|okay"
                       r"|ok'd|okayed|sign\s+off|signed\s+off|reply|replied|respond"
                       r"|responded|answer|answered|give\s+(?:me\s+)?the\s+word|let\s+me\s+know|get\s+back)"
                       r"|i\s+hear\s+(?:from\s+you|back)|(?:i|we)(?:'ve|\s+have)?\s+(?:have|get|got|receive)\s+your"
                       r"|(?:your|a|the)\s+(?:quick\s+)?(?:word|go-?\s?ahead|ok|okay|nod"
                       r"|thumbs[- ]?up|green\s+light|say-?so|reply|answer|confirmation|signal|cue))\b", re.I)


# A REFERENCE to the user's earlier words ("per your instruction", "following
# your brief", "as you asked") is a report of what is being done, never a
# request. "Your X" is needed input only in a frame of need, waiting or
# receipt: "I need your X", "waiting on your X", "once I have your X".
_REFERENCE = re.compile(r"\b(?:per|following|as|according\s+to|in\s+line\s+with|based\s+on|by|from|after|with)\s+"
                        r"(?:all\s+)?(?:your|the)\s+(?:\w+\s+)?$", re.I)
_NEED_FRAME = re.compile(r"\b(?:need|needs|require|requires|waiting\s+(?:on|for)|wait(?:s)?\s+(?:on|for)|await(?:ing|s)?"
                         r"|pending|on|upon|without|(?:once|when|until|after|as\s+soon\s+as)\s+(?:i|we)(?:'ve)?\s+"
                         r"(?:have|get|got|hear|receive)|(?:love|appreciate|welcome|value)|for|want|(?:'d|would)\s+like"
                         r"|keen\s+(?:on|for))\s+"
                         r"(?:a\s+|the\s+)?(?:quick\s+|final\s+|explicit\s+)?$", re.I)


def _needed_input(s: str, m) -> bool:
    """"Your X" at `m` is input the Host needs or waits for: in a frame of
    need, waiting, receipt or wish ("I need your X", "waiting on your X",
    "once I have your X", "keen to hear your thoughts"), named as what is
    missing ("the only thing missing is your sign-off"), said to help the
    Host's step ("your thoughts would help before I …"), or standing alone as
    its own clause ("Committing is next — your say.")."""
    before, after = s[:m.start()], s[m.end():]
    if _NEED_FRAME.search(before) or re.search(r"\b(?:to|and)\s+(?:hear|get|have|see|read|know)\s+$", before, re.I):
        return True
    if re.search(r"\b(?:missing|left|outstanding|needed|required|remaining|lacking)\b[^.;:!?]*?\b(?:is|are)\s+$",
                 before, re.I):
        return True
    if re.match(r"^(?:\s+on\s+[^,.;!?]*?)?\s+(?:would|will|could)\s+(?:really\s+)?(?:help|be\s+(?:great|useful"
                r"|helpful|good|appreciated))\b", after, re.I):
        return True
    return _clause_at(s, m.start()).strip(" .!?—–-").lower() == m.group(0).lower()


def _host_waits(s: str) -> bool:
    """A Host step (or halt, or proposal) governed by a condition on the user
    — the user its subject, or their approval — unless the user does the work
    themselves and the Host's clause is an abstention. The step is read in the
    clause the conditions govern, with the conditions taken out, so a verb
    inside a condition ("… that I don't have") is not the Host's step."""
    found = [(whole, c) for whole, c in _conds(s) if _on_user(c, whole)]
    conds = [c for _, c in found]
    if not conds or _COUNTERFACTUAL.search(s):
        return None                                     # "Had you asked, I would have …"
    if all(_USER_ACTS.search(c) for c in conds) and _ABSTAIN.search(s) and not _REPLY_ASKED.search(s):
        return None
    main = _COND.sub(" ", s)
    if _REPORT_BACK.search(main) and not _ASK_USER.search(main):
        return None                                     # "I'll report the diff when it's reviewed"
    whole, cond = found[0]
    kind = _cond_kind(cond, whole)
    basis = ("the condition read as resting on the user (their act, decision or approval)" if kind == "user"
             else "the condition read as the user's own evaluation of the work" if kind == "evaluated"
             else "the condition read as an approval of the work (the approval lexicon)")
    # A HELD STEP is a future Host commitment about a specific item; "once you
    # approve one, mgmt_run lets me execute it" describes how approvals work.
    on_reply = bool(_ON_REPLY.search(" ".join(conds))) and bool(_FUTURE_HOST.search(main)) \
        and not any(_GENERIC_ITEM.search(c) for c in conds)
    if any(_cond_kind(m.group(1)) == "user" for m in _WAITING_ON.finditer(s)) \
            and re.search(r"\b(?:i|we)(?:'ve|'m|'re)?\s+\w+", main, re.I):
        return _hit(whole, None if on_reply else "the Host halting on " + basis[4:])  # a halt
    if _HOST_ACTS.search(main) or _PROPOSAL.search(main) or _ASK_USER.search(main) or _OFFER.search(main):
        return _hit(whole, None if on_reply else basis)
    return None


def _waits(s: str, quotes: list[str]) -> tuple | None:
    """(b) A Host step waits on the user: the explicit reading when the
    sentence has one, else the first judged one."""
    first = None
    for hit in _wait_hits(s, quotes):
        if hit[1] is None:
            return hit
        first = first or hit
    return first


def _wait_hits(s: str, quotes: list[str]):
    if _real_approval(s):
        return
    for m in _ASK_USER.finditer(s):
        verb = re.search(r"(?:let\s+me\s+know|lmk|tell\s+me|keep\s+me\s+posted|shout|holler"
                         r"|(?:ping|nudge|poke|message|text|buzz)\s+me|give\s+me"
                         r"|send\s+me|drop\s+me|advise|confirm|approve|review|check|pick|choose|select|decide"
                         r"|clarify|specify|share|provide|paste|upload|attach|forward|ask(?:\s+me)?"
                         r"|feel\s+free\s+to\s+\w+(?:\s+\w+)*)$", m.group(0), re.I)
        if not _imperative_at(s, m.end() - len(verb.group(0))):
            continue
        if _LATER.search(_clause_at(s, m.end())) or _points_to_result(s):
            continue
        if re.search(r"\bask\b", verb.group(0), re.I) and not re.search(r"\bask\s+me\s+to\b", s, re.I) \
                and not _names_work(s):
            continue                                    # "ask it directly": a new question, no work named
        objs = _object_clauses(s[m.start():])
        pay = _HOST_PAYOFF.search(s, m.end())
        if objs and all((_DEVIATION.search(_expand(o)) or _NOTICES_SOMETHING.match(_expand(o)))
                        and not _USER_WILL.match(_expand(o)) for o in objs) \
                and not (pay and not _defect_contingency(s, pay.start())):
            continue                                    # "tell me if anything looks off / you spot a typo"
        if pay and _defect_contingency(s, pay.start()):
            continue                                    # "tell me if anything looks off and I'll queue a pass"
        at = m.end() - len(verb.group(0))
        yield _hit(s[at:][:80], None if pay else
                   "a request to the user with no Host step waiting on it" if not objs
                   else "a request to tell, read as not about a problem (the deviation lexicon)")
        break
    imp = _imperative_payoff(s)
    if imp and not _defect_contingency(s, s.find(imp) + len(imp)):
        yield _hit(imp, "an imperative read from its verb and object, with the Host's step after it")
    for rx in (_HAND_OVER, _YOUR_REPLY, _HALT):
        for m in rx.finditer(s):
            clause = _clause_at(s, m.start())
            if _NEG.search(clause[:clause.find(m.group(0))] if m.group(0) in clause else clause):
                continue                                # "Nothing is waiting on you."
            if _past_report(s, m.start()):
                continue                                # "cleared on your word": already given
            if _LATER.search(clause):
                continue                                # "your call later"
            if rx is not _HALT and _REFERENCE.search(s[:m.start()] + ("your " if m.group(0).lower().startswith(
                    "your") else "")):
                continue                                # "per your instruction, I'm writing it up"
            if rx is _YOUR_REPLY and not _needed_input(s, m):
                continue                                # "your X" is needed input only when needed or awaited
            on_reply = (rx is not _HAND_OVER and (_HALT.search(s) or _HOST_ACTS.search(s) or _HOST_PAYOFF.search(s))
                        and (_YOUR_REPLY.search(s) or _ON_REPLY.search(s)))
            yield _hit(m.group(0), None if on_reply else
                       "the decision handed to the user" if rx is _HAND_OVER else
                       "the user's reply wanted, with no Host step held on it" if rx is _YOUR_REPLY else
                       "the Host halting, not on a reply")
            break
    if _BARE_WISH.match(s):
        yield _hit(_BARE_WISH.match(s).group(0), "a bare wish for the user's word")
    # the user handed an evaluation ("… for you to review before I import them"),
    # or the Host wanting their input ("I'd love a second pair of eyes on it")
    for m in _FOR_YOU_TO.finditer(s):
        if not _NEG.search(_clause_at(s, m.start())[:40]):
            yield _hit(m.group(0), None if _HOST_PAYOFF.search(s) else
                       "an evaluation handed to the user")  # still waiting, however it was handed over
            break
    for m in _WANTS_INPUT.finditer(s):
        if not _past_report(s, m.start()) and not _NEG.search(_clause_at(s, m.start())[:40]):
            yield _hit(m.group(0), "the Host wanting the user's input")
            break
    # the Host's progress waits on a user SIGNAL: "pending a thumbs-up", "on
    # your nod", "a quick nod from you and it's committed"
    for m in _COND.finditer(s):
        if m.group(3) and _cond_kind(m.group(3)) == "user" and not _past_report(s, m.start()):
            yield _hit(m.group(0))                      # a Host step on the user's signal
            break
    sig = re.search(r"\b" + _SIGNAL + r"\s+from\s+you\b", s, re.I) \
        or re.match(r"^\W*(?:(?:a|an|one|your)\s+)?(?:quick\s+)?" + _SIGNAL + r"\b(?:\s+(?:here|back))?\s*,?\s*"
                    + _PAYOFF_STEP.pattern, s, re.I)
    if sig and not _past_report(s, 0):
        yield _hit(sig.group(0))
    h = _host_waits(s)
    if h:
        yield h


_PAST_VERB = re.compile(r"\b(?:\w{3,}ed|was|were|had|did|went|came|ran|got|gave|made|took|said|sent|found"
                        r"|kept|left|told|brought|became|began|chose|drew|held|put|set|won)\b", re.I)
_PRESENT_VERB = re.compile(r"\b(?:is|are|am|will|'ll|would|'d|can|could|shall|should|need|needs|waiting|wait"
                           r"|waits|pending|remains?|stays?|sits?|now|still|let|tell|pick|choose|decide|say)\b"
                           r"|'s\b|'re\b", re.I)


def _past_report(s: str, pos: int) -> bool:
    """The phrase at `pos` sits in a past-tense account — a reply already
    given ("cleared on your word"), a status recounted ("diagnose returned
    … your call …") — not a request made now. Its clause (or, when it has no
    verb of its own, the sentence around it) is past and nothing in it is
    present or future."""
    clause = _clause_at(s, pos)
    scope = clause if (_PAST_VERB.search(clause) or _PRESENT_VERB.search(clause)) else s
    outside = re.sub(r"\([^)]*\)", " ", scope) if scope is s else scope
    return bool(_PAST_VERB.search(outside)) and not _PRESENT_VERB.search(outside)


def _points_to_result(s: str) -> bool:
    return bool(_POINTER.search(s)) and not _REPLY_ASKED.search(s) and not _HOST_PAYOFF.search(s)


_OTHER_REPLY = re.compile(r"\b(?:let\s+me\s+know|lmk|tell\s+me|reply|respond|answer|advise|get\s+back\s+to\s+me)\b",
                          re.I)


def _real_approval(s: str) -> bool:
    """A pointer to a real approval surface for a real gate — "The phase is
    waiting for your approval in the Workbench", "Approve the phase in the
    Workbench to ship it" — in any mood, asking nothing else of the user."""
    return bool(_REAL_GATE.search(s) and _REAL_SURFACE.search(s)) and not _OTHER_REPLY.search(s) \
        and not _HOST_STEP.search(s)


# The founding pattern: the user asked to SAY or TYPE a word so the Host
# proceeds. It is an ask whatever else the sentence says — a surface it
# names, a condition it sits in. Only these let it out: an explicit subject
# ("I'd say yes", "you can say go"), a later occasion in its own clause, another
# program's prompt named as the addressee with no Host step after it, and the
# user's own command ("type `ls` to list it").
_SAY_WORD = re.compile(r"\b(?P<v>say|type|send|reply|respond|answer|enter|drop|hit\s+reply)\s+(?:with\s+)?"
                       r"(?:(?:a|an|the|quick|single|below|back|here|me|us)\s+){0,3}(?P<t>" + _TOKEN + r")", re.I)
_EXPLICIT_SUBJECT = re.compile(r"\b(?:(?:i|we|they|he|she|it|who)(?:'d|'ll|\s+(?:would|could|can|will|might|should"
                               r"|may|must|did|do|does))?|you(?:'d|'ll|\s+(?:would|could|can|will|might|should|may|must"
                               r"|did|do|does)))\s+(?:also\s+|just\s+)?$", re.I)


def _says_the_word(s: str, quotes: list[str]) -> bool:
    for m in _SAY_WORD.finditer(s):
        tok = m.group("t")
        if _QTOK.fullmatch(tok) is None and not _assent_token(tok, quotes) \
                and not re.match(r"(?:the\s+word|(?:a\s+)?thumbs)", tok, re.I):
            continue
        if _EXPLICIT_SUBJECT.search(s[:m.start("v")]) or _REPORTED_ASK.search(s[:m.start("v")]):
            continue                                    # "I'd say yes", "you can say go", "asking you to say go"
        if _LATER.search(_clause_at(s, m.start("v"))):
            continue                                    # "next time, just say go"
        payoff = bool(_HOST_PAYOFF.search(s[m.end():]))
        if _OTHER_PROMPT.search(s) and not payoff:
            continue                                    # "when the installer prompts you, type yes"
        if _OWN_PURPOSE.search(s[m.end():]) and not payoff:
            continue                                    # "type `cat …` to see it"
        if _QTOK.fullmatch(tok) and not _assent_token(tok, quotes) and m.group("v").lower() in ("type", "enter", "send") \
                and not payoff:
            continue                                    # typing or sending some text, not a reply to the Host
        return _hit(m.group(0))
    return None


# CORE (the named patterns, what the gate FAILs on): a word the user is
# told to say, type or reply — the verb in imperative mood at the head of a
# clause addressed to the user, or with "you" as its subject ("you say go and
# I'll …") — and a direct question offering or awaiting the Host's step
# ("Want me to …?", "Shall I …?"). "Should say", "it says", "to say" and a
# reported ask describe a word; they do not ask for one.
_CORE_WORD_VERBS = ("say", "type", "reply", "respond", "answer", "enter")
_CORE_Q = re.compile(r"(?:^\W*(?:(?:or|and|so|then|also)\s+)?|,?\s+or\s+)"
                     r"(?:(?:do|would)\s+you\s+(?:want|like)\s+(?:me|us)\s+to|(?:do\s+you\s+)?want\s+(?:me|us)\s+to"
                     r"|shall\s+(?:i|we)|should\s+(?:i|we)(?!\s+have\b)|(?:do\s+you\s+)?need\s+(?:me|us)\s+to"
                     r"|(?:what|which|where|how|when)\s+(?:[\w-]+\s+){0,3}?(?:should|shall)\s+(?:i|we)(?!\s+have\b)"
                     r"|(?:what|which|where|how|when)\b[^?]{0,40}?\b(?:do|would)\s+you\s+(?:want|like)\s+(?:me|us)\s+to"
                     r"|(?:any|which|what)\b(?!(?:thing|one)?\s+else\b)[^?]{0,40}?\byou(?:'d|\s+would)?\s+(?:want|like)"
                     r"\s+(?:me|us)\s+to)\b", re.I)
# A ONE-TOKEN REPLY among enumerated options: "Reply with 1, 2, or 3", "Just
# reply with a number", "Which would you like — 1, 2, or 3?"; and the indirect
# offer question "Let me know if you want me to …".
_OPTIONS = r"(?:\d{1,2}|[A-D])(?:\s*(?:,|or|/)\s*(?:or\s+)?(?:\d{1,2}|[A-D]))+"
_CORE_OPTION_REPLY = re.compile(r"\b(?:reply|respond|answer|say|type|pick|choose)\s+(?:with\s+)?(?:just\s+)?"
                                r"(?:(?:a|the)\s+(?:number|letter)\b|" + _OPTIONS + r"\b)", re.I)
_CORE_OPTION_Q = re.compile(r"\b(?:which|what|pick|choose|prefer|would\s+you\s+like|do\s+you\s+want)\b[^?]*?\b"
                            + _OPTIONS + r"\b", re.I)
_CORE_IF_WANT_ME = re.compile(r"\b(?:let\s+me\s+know|tell\s+me|lmk)\s+(?:if|whether)\s+you(?:'d|\s+would)?\s+"
                              r"(?:want|like|prefer)\s+(?:me|us)\s+to\b", re.I)


def _core_word(s: str, quotes: list[str]) -> str | None:
    """The words of a core reply request in `s` (a word to say / type /
    reply, an option to reply with, "let me know if you want me to …"), or None."""
    for m in list(_CORE_OPTION_REPLY.finditer(s)) + list(_CORE_IF_WANT_ME.finditer(s)):
        if _imperative_at(s, m.start()) and not _LATER.search(_clause_at(s, m.start())) \
                and not _REPORTED_ASK.search(s[:m.start()]):
            return m.group(0)                           # "reply with 1, 2, or 3", "let me know if you want me to …"
    for m in _SAY_WORD.finditer(s):
        if m.group("v").lower() not in _CORE_WORD_VERBS:
            continue
        tok = m.group("t")
        if _QTOK.fullmatch(tok) is None and not _assent_token(tok, quotes) \
                and not re.match(r"(?:the\s+word|(?:a\s+)?thumbs)", tok, re.I):
            continue
        pos = m.start("v")
        if _REPORTED_ASK.search(s[:pos]):
            continue
        # its OWN SUBJECTLESS CLAUSE: the verb right after a clause boundary,
        # whatever clause precedes it ("…is the tool for that, say the word.")
        own_clause = bool(re.search(r"(?:^|[,;:—–]|\s-)\s*(?:(?:just|simply|please|then|so|and|now)\s+)*$",
                                    s[:pos], re.I))
        if not (own_clause or _imperative_at(s, pos)
                or re.search(r"\byou\s+(?:just\s+|simply\s+|only\s+)?$", s[:pos], re.I)):
            continue                                    # "should say", "to say", "it says"
        if _LATER.search(_clause_at(s, pos)):
            continue
        payoff = bool(_HOST_PAYOFF.search(s[m.end():]))
        if (_OTHER_PROMPT.search(s) or _OWN_PURPOSE.search(s[m.end():])) and not payoff:
            continue
        return m.group(0)
    return None


_BARE_CHOICE_Q = re.compile(r"^\W*(?:so\s+|and\s+|then\s+)?which(?:\s+(?:of\s+(?:these|those|them|the\s+[\w-]+)|[\w-]+))?"
                            r"\s+(?:would|do|should|will|'d)\s+you\s+(?:like|want|prefer|pick|choose|go\s+with|rather)\b"
                            r"|^\W*which\s+one\b(?!\s+(?:did|was|were|had)\b)", re.I)


def _core_question(q: str) -> bool:
    """A direct question to the user offering or awaiting the Host's step —
    at its head, after a lead-in, or as the "or" alternative ("…, or should
    I escalate it?") — or a choice among enumerated options."""
    if not _is_question(q):
        return False
    parts = [q] + re.split(r":\s+|\s+[—–]\s+", q)
    # a salutation or interjection before it: "Hi Alex, want me to …?", "OK, should I …?"
    parts += [re.sub(r"^\W*(?:hi|hey|hello|ok|okay|so|alright|right|great|cool|now)\b[^,?]{0,30},\s*", "", p, flags=re.I)
              for p in parts]
    return any(_CORE_Q.search(p) for p in parts) or bool(_CORE_OPTION_Q.search(q))


def _flag(s: str, quotes: list[str]) -> str | None:
    """The rule a statement breaks, or None; core when the sentence holds one
    of the named patterns."""
    got = _flag_rule(s, quotes)
    core = _core_word(s, quotes)
    if core and got is None:
        return Fired(L_WORD, _hit(core), core=True)     # read before every way out, like "say go"
    if core and not got.core:
        return Fired(str(got), (got.why, got.basis), core=True)
    return got


def _flag_rule(s: str, quotes: list[str]) -> str | None:
    hit = _says_the_word(s, quotes)
    if hit:
        core = _core_word(s, quotes)
        return Fired(L_WORD, hit if core else (hit[0], "a word to say, described rather than asked of the user"),
                     core=core)
    hit = _readiness_gate(s) or _reply_then_step(s)
    if hit:
        return Fired(L_WAIT, hit)
    offer = _offers(s)
    if offer and _bare_reply_request(s):
        return Fired(L_OFFER, (offer[0], None))         # "I could also commit it — just say."
    proposal = re.match(r"^\s*((?:[\w'-]+\s+){0,3}[\w'-]+)\s*:\s+(\S.*)$", s)
    if proposal and re.search(r"\b(?:next\s+steps?|propos\w*|recommend\w*|suggest\w*)\b", proposal.group(1), re.I) \
            and _proposes(proposal.group(2)):
        return Fired(L_OFFER, _hit(proposal.group(0)[:80], "a proposed next step that names an action"))
    if _real_approval(s):
        return None
    hit = _needs_from_you(s)
    if hit:
        return Fired(L_WAIT, hit)
    parts = [p for p in re.split(r"\s+[—–]\s+|;\s+|\s-\s", s) if re.search(r"\w", p)]
    if len(parts) > 1:
        # a condition governs its own clause: "I stopped before touching the
        # database — say the word and I'll apply it" asks in its second clause
        got = [g for g in (_flag(p, quotes) for p in parts) if g]
        if got:
            return next((g for g in got if g.explicit), got[0])
        if (_contingent(s) and not (_scheduled(s) and _imperative_payoff(s))) or _NEW_REQUEST.search(s):
            return None
    if _NEW_REQUEST.search(s) and _requests(s) <= 1:
        return None                                     # an invitation to a new request, and nothing else asked
    if _contingent(s) and not (_scheduled(s) and _imperative_payoff(s)):
        return None                                     # a contingency; a request timed by an expected event still asks
    other_prompt = bool(_OTHER_PROMPT.search(s)) and not _HOST_PAYOFF.search(s)
    hit = None if other_prompt else _asks_reply(s, quotes)
    if hit:
        return Fired(L_WORD, hit, core=_core_word(s, quotes))
    hit = _offers(s)
    if hit:
        return Fired(L_OFFER, hit)
    hit = _waits(s, quotes)
    if hit:
        return Fired(L_WAIT, hit)
    return None


# --- questions ------------------------------------------------------------------------
# A TAG QUESTION closing a statement ("Simple enough, right?", "That was
# quick, wasn't it?") asks the user to agree with the statement. It is an ask
# only if the statement is one, so the statement is read without its tag.
_TAG = re.compile(r",?\s*(?:right|eh|yeah|yes|no|ok|okay|correct|huh|true|innit|wouldn't\s+you\s+say|don't\s+you\s+think"
                  r"|(?:is|isn't|was|wasn't|are|aren't|were|weren't|does|doesn't|do|don't|did|didn't|will|won't"
                  r"|would|wouldn't|can|can't|could|couldn't|has|hasn't|have|haven't|should|shouldn't)"
                  r"(?:\s+(?:it|they|you|we|i|that|there|he|she|this))?)\s*\?+\W*$", re.I)


def _tag_statement(q: str) -> str | None:
    """The statement a tag question tags, or None when it is not one (no
    comma before the tag, or a head that is itself a question)."""
    m = _TAG.search(q)
    if not m or not m.group(0).startswith(","):
        return None
    head = q[:m.start()].strip()
    if not re.search(r"\w", head) or _ASK_OPEN.match(head) or _Q_SUBJECT.match(head) \
            or re.match(r"^\W*(?:what|which|who|where|when|how|why)\b", head, re.I):
        return None
    return head + "."


def _is_question(s: str) -> bool:
    return bool(_QUESTION.search(s))


def _q_kind(q: str) -> str:
    """'social', 'capability', 'faq_i', 'fact', or 'user' (the default). A
    question after a lead-in ("The only open item: shall I push it?") is
    typed by the question itself as well as by the whole: either one to the
    user makes it the user's."""
    parts = [p for p in re.split(r":\s+|\s+[—–]\s+", q) if re.search(r"\w", p)]
    kinds = [_q_kind_one(q)] + ([_q_kind_one(parts[-1])] if len(parts) > 1 else [])
    if "user" in kinds:
        return "user"
    return kinds[-1]


def _q_kind_one(q: str) -> str:
    body = re.sub(r"^[^\w]+", "", q)
    low = body.lower()
    if re.match(r"^(?:can|could|would|will|does|do|did|is|are|has|have)\s+(?:someone|anyone|anybody|somebody"
                r"|everyone|everybody)\b", low):
        return "other"                                  # to a third party (a message's audience)
    words = low.split()
    first = re.sub(r"[^\w']", "", words[0]).split("'")[0] if words else ""
    if _SOCIAL.search(low) and not re.search(r"\b(?:file|build|project|code|readme|tests?|commit|phase"
                                             r"|approval|draft|design|change)\b", low):
        return "social"
    if re.search(r"\b(?:you|your|yours|yourself|y'all)\b", low):
        if re.match(r"^(?:can|could)\s+you\s+(?!" + _REPLY_VERBS_Q + r"\b)\w+", low):
            return "capability"
        return "user"
    if re.match(r"^should\s+(?:i|we)\s+have\b", low):
        return "fact"                                   # counterfactual about the past
    if re.match(r"^(?:can|may)\s+i\b", low):
        return "faq_i"
    if re.match(r"^(?:should|shall)\s+(?!(?:i|we)\s+have\b)\w+", low) \
            or re.match(r"^(?:should|shall|could|might|would)\s+(?:i|we)\b", low) \
            or re.search(r"\b(?:want|need)\s+(?:me|us)\s+to\b", low):
        return "user"                                   # proposes the Host's own action
    qm = _Q_SUBJECT.match(body)
    if qm and qm.group("aux").lower() not in _PAST_AUX and _EVALUATIVE.search(body[qm.start("subj"):]):
        return "user"                                   # asks the user's judgement
    if first in ("which",) or re.match(r"^what\s+(?:next|else|now)\b", low):
        return "user"                                   # a choice
    if qm or (first in _WH_FACT) or first == "the":
        return "fact"
    return "user"                                       # no stated subject


_STOP = {"there", "that", "this", "with", "have", "what", "when", "where", "which", "does", "will", "would",
         "could", "should", "been", "were", "your", "from", "into", "then", "than", "they", "them", "just",
         "some", "also", "more", "only", "here"}


def _answered(question: str, reply: str, quotes: list[str]) -> bool:
    """A real answer to THIS question, from the sentence right after it."""
    reply = reply.strip()
    if _is_question(reply):
        return False
    kind = _q_kind(question)
    parts = [p for p in re.split(r":\s+|\s+[—–]\s+", question) if re.search(r"\w", p)]
    q = re.sub(r"^[^\w]+", "", parts[-1] if parts else question)
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(reply) if c.strip()]
    if kind == "capability":
        return bool(_OPENERS.match(clauses[0]) or re.match(r"^you\s+(?:can|could)\b", reply, re.I))
    if kind == "faq_i":
        return bool(_FACT_OPENERS.match(clauses[0]) or re.match(r"^you\b", reply, re.I))
    if kind != "fact":
        return False
    first = re.sub(r"[^\w']", "", q.split()[0].lower()).split("'")[0] if q.split() else ""
    qm = _Q_SUBJECT.match(q)
    if first in _WH_FACT or first == "the" or not qm:
        return _flag(reply, quotes) is None and not _COUNTERFACTUAL.search(reply) \
            and not re.search(r"\b(?:i|we)(?:'ll|\s+will|'d|\s+would|\s+can|\s+could)\b", reply, re.I)
    if _FACT_OPENERS.match(clauses[0]) or re.match(r"^(?:nothing|none|nobody|no\s+\w+|never|not\b)", clauses[0], re.I):
        return True                                     # its polarity
    if _flag(reply, quotes) is None and not re.search(r"\b(?:would|could|might|'d)\b", reply, re.I):
        if re.search(r"\bnot\b", _expand(clauses[0]), re.I):
            return True                                 # a negated fact: "The workspace isn't a repo."
        qwords = {w for w in re.findall(r"[a-z0-9]{4,}", q.lower()) if w not in _STOP}
        if qwords & {w for w in re.findall(r"[a-z0-9]{4,}", reply.lower())}:
            return True                                 # its value: "That's exactly what it holds."
    qsubj = qm.group("subj").lower().split()
    for c in clauses:
        am = _A_SUBJECT.match(c)
        if not am:
            continue
        verb = (am.group(2) or am.group(3) or "").lower()
        asubj = am.group(1).lower().split()
        same = asubj == qsubj or (len(qsubj) == 2 and asubj == ["it"]) or (qsubj == ["that"] and asubj == ["it"])
        if not same:
            continue
        if _MODAL.match(verb) and verb != qm.group("aux").lower():
            continue
        return True
    return False


# A WH-QUESTION THAT LOST ITS "?": interrogative order (a wh-word, then an
# auxiliary or copula, or which/what + noun + auxiliary + subject), no colon
# (not "What it holds: …"), no second verb after it (not "What's first is …"),
# not a discourse form ("Which is why …", "What's more …").
_WH_Q = re.compile(r"^\W*(?:(?:what|which|who|where|when|how|why)(?:'s|'re|\s+(?:is|are|was|were|do|does|did|should"
                   r"|shall|will|would|can|could|might|must))\b|(?:what|which)\s+(?:[\w-]+\s+){1,5}?(?:do|does|did"
                   r"|should|shall|will|would|can|could)\s+(?:you|i|we)\b)", re.I)
_WH_DISCOURSE = re.compile(r"^\W*(?:which\s+(?:is|was)\s+(?:why|how|what|where|when)|which\s+(?:means|meant)"
                           r"|what'?s\s+more|what\s+is\s+more)\b", re.I)


def _wh_question(s: str) -> bool:
    m = _WH_Q.match(s)
    return bool(m) and ":" not in s and not _WH_DISCOURSE.match(s) \
        and not re.search(r"\b(?:is|are|was|were)\b", s[m.end():], re.I) and len(s.split()) <= 14


def _explicit_question(q: str) -> bool:
    """A direct question to the user offering or awaiting the Host's step: an
    offer form ("want me to", "shall I", "would you like"), the Host or the
    user named in it, or a "should / shall" choice. A question with neither
    ("Interested in a README?") is a judged reading."""
    core = re.sub(r"^[^\w]+", "", re.split(r":\s+|\s+[—–]\s+", q)[-1])
    qm = _Q_SUBJECT.match(core)
    return bool(_ASK_OPEN.match(core) or _ASK_OPEN.match(q)
                or re.search(r"\b(?:you|your|yours|i|me|my|we|us|our|let's)\b", q, re.I)
                or re.match(r"^(?:should|shall)\b", core, re.I)
                # the user's judgement of the work: "Does that look right?"
                or (qm and qm.group("aux").lower() not in _PAST_AUX and _EVALUATIVE.search(core[qm.start("subj"):]))
                # "you" elided before a wish: "Interested in a README?", "Ready for the next one?"
                or re.match(r"^(?:interested|keen|ready|happy|up)\s+(?:in|to|for|with)\b", core, re.I))


def _open(units: list[dict], ui: int, si: int) -> bool:
    """The question at units[ui].sentences[si] is still open: nothing right
    after it answers it (the next sentence of its unit, or — for a heading,
    bold line, 'Q:' line or blockquote — the first sentence of the next unit)."""
    u = units[ui]
    s = u["sentences"][si]
    kind = _q_kind(s)
    if kind in ("social", "other"):
        return False
    nxt, nq = None, u["quotes"]
    if si + 1 < len(u["sentences"]):
        nxt = u["sentences"][si + 1]
    elif (u["quote"] or u["standalone"]) and ui + 1 < len(units):
        nxt, nq = units[ui + 1]["sentences"][0], units[ui + 1]["quotes"]
    return nxt is None or not _answered(s, nxt, nq)


# A wh-question head: "What's / What name / Which branch / Where should …" —
# not "When the installer prompts you, …" or "What I did …".
_WH_HEAD = re.compile(r"^\W*(?:(?:what|which|who)(?:'s|\s+(?!(?:i|we|you|it|they|he|she)\b)\w+)"
                      r"|(?:where|when|how)(?:'s|\s+(?:is|are|was|were|do|does|did|should|shall|will|would|can"
                      r"|could|much|many|long|soon)\b))", re.I)


def _proposes(item: str) -> bool:
    """A list item that proposes an action ("Add a README", "Fix the key and
    retry") — not a report ("encoding: UTF-8", "none, it's in place")."""
    t = item.strip()
    if re.match(r"^[\w' -]{1,30}:\s", t):
        return False                                    # key: value
    first = (re.match(r"^\W*([\w']+)", t) or [None, ""])[1].lower()
    return bool(first) and first not in _STATEMENT_OPENERS and first not in ("n/a", "nil") \
        and not _PAST_VERB.fullmatch(first) and first not in _IRREGULAR_PAST


def find_ask(text: str) -> tuple[str, str] | None:
    """(rule, the sentence that asked) for the ask in the reply the gate is
    surest of — the first core one, else the first explicit, else the first
    — or None. The rule is
    a Fired label: `.why` holds the words it fired on, `.sure` how firm the
    reading is, `.explicit` whether it is one of the explicit forms."""
    best = None
    rank = {"core": 0, "explicit": 1, "judged": 2}
    for fired, u, sentence in _find_asks(text):
        fired.why = _unmask(u, fired.why)
        if fired.core:
            return fired, sentence
        if best is None or rank[fired.level] < rank[best[0].level]:
            best = (fired, sentence)
    return best


# "Let's X" NARRATING what the reply then reports done: "Let's get it done: I
# wrote it …", "Let's verify it landed. I read it back …".
_REPORT_AFTER = re.compile(r"^\W*(?:(?:i|we)\s+(?:just\s+|also\s+)?(?:[a-z]+ed|have|'ve|had|wrote|read|ran|made|put|built"
                           r"|found|got|saw|set|did|kept|left|sent|took)\b"
                           r"|[^.!?]*\b(?:is|are|was|were|holds?|contains?|sits?|lives?)\b(?![^.!?]*\b(?:i|we)"
                           r"(?:'ll|\s+will|\s+can|\s+could)\b))", re.I)


def _lets_narrates(s: str, nxt: str | None) -> bool:
    """`s` opens with "let's" + an action and what follows it — the rest of
    the sentence after a colon or dash, or the next sentence — reports it."""
    m = re.match(r"^\W*let's\s+([^:—–.!?]*)(?:[:—–]\s*(.*)|[.!]*)\s*$", s.strip(), re.I)
    if not m or _FUTURE_WORK.search(m.group(1)) or _is_question(s):
        return False
    after = m.group(2) if m.group(2) else (nxt or "")
    return bool(after) and bool(_REPORT_AFTER.match(after)) and _flag(after, []) is None


def _find_asks(text: str):
    blocks = _prose_blocks(text)
    every = [u for b in blocks for u in _units(b)]
    last = len(every)
    for ui, u in enumerate(every):
        # a lead-in's items, even across the blank line that often follows it
        if u["text"].endswith(":") and not u["item"] and not u["lead_items"]:
            j = ui + 1
            while j < len(every) and every[j]["item"]:
                u["lead_items"].append(every[j])
                j += 1
        for it in u["lead_items"]:
            lead = u["sentences"][-1].rstrip(":").rstrip()
            joined = lead + " " + it["text"]
            inner = _flag(joined, u["quotes"] + it["quotes"])
            if inner:
                yield Fired(L_LEAD, (f"{inner.why} (item read with its lead-in: {inner})",
                                     None if inner.explicit else inner.basis), core=inner.core), u, \
                    _unmask(u, u["sentences"][-1])
                break
            if _ASK_OPEN.match(lead):
                yield Fired(L_LEAD, _hit(lead), core=bool(_CORE_Q.match(lead))), u, _unmask(u, u["sentences"][-1])
                break
            if _LEAD_OFFER.search(lead) and not _REPORTED.search(lead) \
                    and any(_proposes(x["text"]) for x in u["lead_items"]):
                yield Fired(L_LEAD, _hit(lead, "a next-steps lead-in whose items propose actions")), u, \
                    _unmask(u, u["sentences"][-1])
                break
        if u.get("heading") and ui < len(every) - 1 and not u["text"].rstrip().endswith("?"):
            continue                                    # a heading names a topic: "Why I asked you to say go"
        for si, s in enumerate(u["sentences"]):
            nxt = u["sentences"][si + 1] if si + 1 < len(u["sentences"]) else (
                every[ui + 1]["sentences"][0] if ui + 1 < len(every) and every[ui + 1]["sentences"] else None)
            if _is_question(s):
                stated = _tag_statement(s)
                if stated is not None:
                    rule = _flag(stated, u["quotes"])   # a tag question asks what its statement asks
                    if rule:
                        yield rule, u, _unmask(u, s)
                    continue
                if _open(every, ui, si):
                    kind = _q_kind(s)
                    explicit = kind == "user" and _explicit_question(s)
                    # "Which would you like?" after a list of options above it: the
                    # same named pattern as "Which would you like — 1, 2, or 3?"
                    # ... and any such choice question that closes the reply, options
                    # in a list or in prose ("…now, or hold it. Which would you like?")
                    closing = ui == len(every) - 1 and si == len(u["sentences"]) - 1
                    after_options = bool(_BARE_CHOICE_Q.match(s)) and (
                        closing or sum(1 for x in every[:ui] if x["item"]) >= 2)
                    yield Fired(L_QUESTION, core=_core_question(s) or after_options, hit=_hit(_unmask(u, s)[-80:], None if explicit else
                                                 f"a {kind.replace('_', ' ')} question read as unanswered "
                                                 f"by the sentence after it" if kind != "user" else
                                                 "a question with neither the user nor the Host named in it")), \
                        u, _unmask(u, s)
                continue
            rule = _flag(s, u["quotes"])
            if rule:
                if rule == L_OFFER and _lets_narrates(s, nxt):
                    continue                            # "Let's verify it landed. I read it back …"
                yield rule, u, _unmask(u, s)
                continue
            if _WH_HEAD.match(s) and _HOST_STEP.search(s):
                yield Fired(L_QUESTION, _hit(_WH_HEAD.match(s).group(0) + " … " + _HOST_STEP.search(s).group(0),
                                             "a question that lost its '?', ending in the Host's step")), u, \
                    _unmask(u, s)                       # "What's the new number — and I'll lock it in"
                continue
            tail = _CLAUSE_SPLIT.split(s)[-1]
            if (_ASK_OPEN.match(s) or _ASK_OPEN.match(tail)) and _open(every, ui, si):
                op = _ASK_OPEN.match(s) or _ASK_OPEN.match(tail)
                yield Fired(L_QUESTION, _hit(op.group(0), "a question opener without its '?'")), u, _unmask(u, s)
                continue
            if ui == len(every) - 1 and si == len(u["sentences"]) - 1 and _wh_question(s) \
                    and _q_kind(s + "?") not in ("social", "other"):
                yield Fired(L_QUESTION, _hit(_unmask(u, s)[-80:], "a wh-question in interrogative order, "
                                             "ending the reply, without its '?'")), u, _unmask(u, s)


def asks_for_go_ahead(text: str) -> str | None:
    """The rule the first ask in the reply breaks, or None."""
    found = find_ask(text)
    return found[0] if found else None



def ask_form(text: str) -> str | None:
    """The rule the first ask in the reply breaks, or None — the gate's
    asks_for_go_ahead, verdict for verdict."""
    if not isinstance(text, str):
        return None
    try:
        return asks_for_go_ahead(text)
    except Exception:  # noqa: BLE001 — a broken guard never takes a turn down
        return None


def find_go_ahead_ask(text: str) -> str | None:
    """What the reply's ending does that hands the next step back — the
    gate's rule label for the ask it is surest of (find_ask) — or None. Pure
    and total: any input returns a string or None, never raises."""
    hit = find_go_ahead_hit(text)
    return hit[0] if hit else None


def find_go_ahead_hit(text: str):
    """(rule label, the sentence that fired, level) for the reply's ask, or
    None — what the correction quotes, so the Host judges exactly what fired.
    `level` is the rule's own: "core" ⊂ "explicit" ⊂ any; everything else is
    "judged". Only a judged reading may be waved off as "not an ask"; core and
    explicit forms are done or asked in the box. A rule that carries no level
    counts as "explicit" — the safe side.
    The same with a card open: every reading counts, at any
    level, question or statement alike — a card already in front of the
    person changes the CORRECTION (it names that card), never what counts.
    Pure and total."""
    if not isinstance(text, str):
        return None
    try:
        hit = find_ask(text)
    except Exception:  # noqa: BLE001 — a broken guard never takes a turn down
        return None
    if not hit:
        return None
    return str(hit[0]), str(hit[1]), level_of(hit[0])


def level_of(label) -> str:
    """The rule's level for a fired label: "core", "explicit" or "judged"
    (unknown -> "explicit", never "judged")."""
    lv = getattr(label, "level", None)
    if lv in ("core", "explicit", "judged"):
        return lv
    if getattr(label, "core", False):
        return "core"
    return "explicit" if getattr(label, "explicit", True) else "judged"


# For the handoff check below only: a real approval surface — where the
# platform itself asks the user and the code enforces the answer (a gate='user'
# phase in the Workbench, Code Approval, a card in Tickets, the Network app, a
# device approval). A note that names one beside an approval word points at it.
_SURFACE = re.compile(
    r"(?i)\b(?:workbench|tickets|the\s+card|code\s+approval|the\s+button|"
    r"network\s+app|authenticator|your\s+(?:device|phone|bastion)|"
    r"approve\s+button|merge\s+button|auto-?approve)\b")
_APPROVAL_WORD = re.compile(r"(?i)approv|confirm|sign[-\s]?off")


# ── the handoff ─────────────────────────────────────────────────────────────
# The same failure, written down. A turn that ended "Say go" also sent
# close_turn with open_work "Awaiting user go on the theme concepts" and next_step "On user yes: build(...)". The handoff is what the
# NEXT turn starts from (close_turn.thread_state: "Next step (from the last
# turn): …", "Open work: …"), so a recorded wait teaches the next turn to wait
# too — and a reply that asks nothing out loud can still end the turn on one.
# Notes are written about the user, not to them, so they have their own shapes.

_WAIT_ON = (r"(?:go|go[-\s]?ahead|yes|ok|okay|approval|confirmation|decision|"
            r"pick|choice|sign[-\s]?off|green[-\s]?light|answer|reply|say[-\s]?so|input)")
_WHO = r"(?:the\s+)?(?:user'?s?|their|your|a|an)"
_HANDOFF_WAITS: tuple[re.Pattern, ...] = (
    # "Awaiting user go on the 5 theme concepts", "waiting for the user's
    # approval", "pending confirmation", "blocked on their pick".
    re.compile(rf"(?i)\b(?:await(?:s|ing)?|wait(?:s|ing)?\s+(?:on|for)|pending|"
               rf"blocked\s+on|until)\s+(?:{_WHO}\s+)?{_WAIT_ON}\b"),
    # "On user yes: build(...)", "after the user's go", "once their OK".
    re.compile(rf"(?i)\b(?:on|after|once|if|when|upon)\s+{_WHO}\s+{_WAIT_ON}\b"),
    # "User to pick a theme", "user must confirm".
    re.compile(r"(?i)\buser\s+(?:to|must|should|needs\s+to)\s+"
               r"(?:confirm|approve|pick|choose|decide|say|reply|sign\s+off|green-?light)\b"),
    # "once the user confirms", "if the user says go".
    re.compile(r"(?i)\b(?:if|once|when|after)\s+(?:the\s+)?user\s+"
               r"(?:says?|confirms?|approves?|picks?|chooses?|replies|agrees|okays?|oks?)\b"),
)
# A handoff item that names the ticket IS the box's durable record: the ask
# was made the right way (in this turn or an earlier one) and the ticket
# carries it.
_NAMES_TICKET = re.compile(r"(?i)\bticket\b")
# After an ask in the box this turn, a wait that refers to the box, its card
# or the escalate is the box working; any other wait is still
# one.
# It has to be the thing awaited ("… in the question box", "… on the card",
# "… on the escalation"), not the word inside another noun ("Awaiting user go
# on the card design").
_NAMES_BOX = re.compile(
    r"(?i)\b(?:in|on|via|through|from|at)\s+(?:the|their|your|its|this|that)\s+"
    r"(?:question\s+box|box|(?:ask\s+)?card|escalation|ticket)"
    r"(?=\s*(?:$|[.,;:!?)(#—–-]|\s+(?:above|below|for\b|about\b|#?\d)))")


def find_go_ahead_wait(handoff, *, asked_in_box: bool = False) -> str | None:
    """The handoff item (next_step or an open_work entry) that records the
    turn as waiting on the user's go-ahead, or None. The ways out: the item
    names the ticket; after an ask in the box this turn, it refers to the box,
    its card or the escalate; a real approval surface named beside an approval
    word. Pure and total."""
    if not isinstance(handoff, dict):
        return None
    try:
        items = []
        if isinstance(handoff.get("next_step"), str):
            items.append(handoff["next_step"])
        items += [x for x in (handoff.get("open_work") or []) if isinstance(x, str)]
        for item in items:
            if _NAMES_TICKET.search(item):
                continue
            if asked_in_box and _NAMES_BOX.search(item):
                continue
            for pat in _HANDOFF_WAITS:
                m = pat.search(item)
                if not m:
                    continue
                if _APPROVAL_WORD.search(m.group(0)) and _SURFACE.search(item):
                    continue
                return item.strip()
        return None
    except Exception:  # noqa: BLE001
        return None


# ── a reply that stops on its own announced work ────────────────────────────
# A Host ended a rehearsal turn with "Next, I'll draft the permission… Then
# I'll go on to items 2, 3 and 5" and stopped, when it should not have. The
# rule above counts what a reply ASKS, and that reply asks nothing, so it
# passed. It still ends the turn on work the Host has
# not done, and the next turn starts only when the person writes again: the
# same cost as a go-ahead. A guard addition, outside the ported rule (the ship
# gate reads asks; this reads deferral), and only where no card is open (a
# Host whose next step waits on a card's answer is waiting on the person).
_DEFER_LEAD = (r"(?:next(?:\s+up)?(?:\s+turn)?|then|after\s+(?:that|this)|afterwards|from\s+there"
               r"|in\s+(?:my|the)\s+next\s+turn|on\s+the\s+next\s+turn)")
_FUTURE_I = r"(?:I|we)(?:['’]ll|\s+will|\s+am\s+going\s+to|['’]m\s+going\s+to|\s+are\s+going\s+to)"
_DEFER = re.compile(
    rf"(?:^|(?<=[,;:(—–-])\s*|\band\s+)(?:{_DEFER_LEAD})\s*[,:]?\s+{_FUTURE_I}\s+(?:also\s+|then\s+)?"
    rf"(?P<verb>[a-z]+)"
    rf"|^{_FUTURE_I}\s+(?:next|then)\s+(?P<verb2>[a-z]+)"
    # "My next step is to draft…"
    rf"|^(?:my|our)\s+next\s+(?:step|move|job|task)\s+(?:is|will\s+be)\s+(?:to\s+)?(?P<verb3>[a-z]+)"
    # "I'll draft it next.", "I'll pick up items 2-5 in the next turn."
    rf"|^{_FUTURE_I}\s+(?P<verb4>[a-z]+)\b(?=[^.!?]*?(?:\snext\s*[.!]?$"
    rf"|\b(?:in|on)\s+(?:the|my)\s+next\s+(?:turn|reply|message)\b))", re.I)
# "I'll show you what comes next": the word names something else, not when.
_WHAT_NEXT = re.compile(r"\b(?:what|which|who|where|how)\b[^.!?]*\bnext\b", re.I)
# Steps that report back or watch (they wait on something running), and words
# that end the reply rather than name work ("Then I'll be done", "Then I'll
# hand it back to you", "Then we'll see").
_DEFER_REPORTS = frozenset({"report", "tell", "let", "watch", "wait", "monitor", "ping",
                            "notify", "remind", "be", "stop", "leave", "hand", "see", "know",
                            "hear"})
# Verbs that report only with these words after them: "check back", "get back
# to you", "update you", "post the results"; "check the other boxes", "come back
# to items 2 and 3" and "update the rule" are the Host's own work.
_DEFER_REPORT_PHRASE = re.compile(
    r"^(?:check|follow|get|come|circle)\s+(?:back|in)\b(?!\s+to\s+(?!(?:you|me)\b))"
    r"|^(?:check|follow)\s+(?:up\s+)?(?:on|with)\s+(?:you|it|them|that|this|progress|how"
    r"|the\s+(?:run|build|scan|job|task|agents?|results?))\b"
    r"|^(?:update|keep)\s+you\b"
    r"|^keep\s+(?:an\s+eye|watch|track|watching|monitoring)\b"
    r"|^post\s+(?:an?\s+|the\s+|my\s+)?(?:update|results?|summary|report|findings|progress)\b",
    re.I)
# The reply saying what it says next, with the words right there: "Next, I'll
# explain how it works: …", "Then I'll show you the diff below."
_DEFER_SAYS = re.compile(
    r"^(?:show|walk|explain|summari[sz]e|answer|describe|outline|recap|go\s+over|lay\s+out"
    r"|break\s+down|list)\b[^.!?]*?(?::\s*\S|\b(?:below|above|here)\b)", re.I)
# A step tied to a time or to something else finishing is scheduled, not put off.
_DEFER_SCHEDULED = re.compile(
    r"\b(?:when|once|as\s+soon\s+as|until|whenever)\b"
    r"|\bafter\s+(?:it|they|the|your|you)\b"
    r"|\b(?:tomorrow|tonight|later\s+today|this\s+(?:evening|afternoon|morning)"
    r"|at\s+\d|in\s+(?:an?|\d+)\s+(?:minute|hour|day)s?|on\s+(?:mon|tue|wed|thu|fri|sat|sun)\w*"
    r"|next\s+(?:week|month|year|(?:mon|tues|wednes|thurs|fri|satur|sun)day)"
    r"|in\s+(?:january|february|march|april|may|june|july|august|september|october|november"
    r"|december))\b",
    re.I)
DEFER_RULE = "puts off its own next step to a later turn, which starts only when the user writes again"


def find_deferred_work(text: str) -> tuple[str, str] | None:
    """(DEFER_RULE, the sentence) when the reply's closing part puts off the
    Host's own next step to a later turn ("Next, I'll draft…", "Then I'll go on
    to items 2, 3 and 5", "Next turn I'll open the finding"), or None. A step
    that reports back or watches, or that is tied to a time or to something
    else finishing, is not put off. Quoted words are not the Host's, and a list
    item is a plan's step, which the person may have asked to read rather than
    to have run. Pure and total."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        for u in closing_units(text):
            if u.get("quote") or u.get("item"):
                continue
            for s in u["sentences"]:
                m = _DEFER.search(s)
                if not m:
                    continue
                g = next(n for n in ("verb", "verb2", "verb3", "verb4") if m.group(n))
                verb = m.group(g).lower()
                if (verb in _DEFER_REPORTS or _DEFER_REPORT_PHRASE.match(s[m.start(g):])
                        or _DEFER_SAYS.match(s[m.start(g):]) or _DEFER_SCHEDULED.search(s)
                        or (g == "verb4" and _WHAT_NEXT.search(s))):
                    continue
                return DEFER_RULE, _unmask(u, s)
        return None
    except Exception:  # noqa: BLE001 — a broken guard never takes a turn down
        return None


def _defer_correction(seen: str, fired: str, *, close_turn_recorded: bool,
                      box_only: bool) -> str:
    """correction() for find_deferred_work. Its sentence asks the user for
    nothing, so the go-ahead correction's third answer ("if that sentence does
    not in fact ask the user for anything … end the turn") would let out the
    very ending it fired on; and "their request is the go-ahead" would send a
    Host that wrote a plan the person asked to read off to run it. Here the
    third answer is about the STEP: not the Host's to take now, or not asked
    for."""
    ct_line = ("\nYour close_turn is recorded. If you end with close_turn again, "
               "carry only the handoff — what YOU do next; only its handoff is kept.\n"
               if close_turn_recorded else "")
    if box_only:
        options = (
            "Now do one of these — `escalate` is the one call that runs now:\n"
            "1. If a real choice remains that only the user can make before the "
            "step, call `escalate` with the options (each with its consequence).\n"
            "2. Otherwise end the turn now, adding nothing: reply with no text at all.\n")
    else:
        options = (
            "Now, in this turn, do one of these:\n"
            "1. DO THE WORK — make the calls the step needs this round, then say "
            "what you started and its id.\n"
            "2. If a real choice remains that only the user can make before the "
            "step, call `escalate` with the options (each with its consequence). "
            "That is the question box: the user answers there and the answer "
            "comes back into this turn.\n"
            "3. If the step is not yours to take now — it waits on something still "
            "running, on an answer the user has not given, or on a set time; or "
            "the user asked for a plan to read, not for the work; or the sentence "
            "describes work already done — end the turn now, adding nothing: "
            "reply with no text at all (or with close_turn alone).\n")
    return (
        "[SYSTEM — do not mention this message to the user]\n"
        "You just wrote, and THE USER HAS ALREADY SEEN, this:\n\n"
        f"---\n{seen}\n---\n\n"
        "This sentence in it was flagged as putting off a step of your own to a "
        f"later turn:\n\n> {fired or seen[-300:]}\n\n"
        "If it does, the turn ends with that step not done, and the next turn "
        "starts only when the user writes again. If they asked you to do this "
        "work, their request already covers the step.\n\n"
        "That text is already on their screen and cannot be unsent, so whatever "
        "you write next is APPENDED to it. Do not repeat it — continue from it.\n\n"
        + options + ct_line +
        "\nNever end a turn on a step you could take now."
    )


# ── what the model is told ──────────────────────────────────────────────────

# ── cards: a question already in front of the person ────────────────────────
# Platform tools whose RESULT carries the ask the platform filed — the same
# named set the client reads cards from (app-web-kit/ask.ts RESULT_ASK_TOOLS).
RESULT_ASK_TOOLS = frozenset({"pipeline", "mgmt_run", "context_ingest", "turn_type"})
# Every tool askFromTool raises a card for — the drift test reads that
# function's syntax tree (scripts/ask_card_tools.cjs) and requires each name
# here.
CARD_TOOLS = frozenset({"escalate"}) | RESULT_ASK_TOOLS


def _card_options(src) -> int:
    """How many options a person could click — askFromTool's own test: a dict
    with a non-empty string id. A card with none is never shown."""
    opts = src.get("options") if isinstance(src, dict) else None
    return sum(1 for o in (opts if isinstance(opts, list) else [])
               if isinstance(o, dict) and isinstance(o.get("id"), str) and o.get("id"))


def card_from_tool(name: str, args, result) -> dict | None:
    """The card this tool call put in front of the person, or None — read the
    way the client raises one (app-web-kit/ask.ts askFromTool): an `escalate`
    that filed an OPEN ticket; a RESULT_ASK_TOOLS call (or a registry op of
    that name run through `operate`) whose result carries the ask it filed;
    and a `build` the gate refused because a phase awaits the person's
    approval, which raised that phase's approval card. Like the client, an
    ask with no option a person could click raises no card.
    {"ref": how the correction names it, "where": where it waits,
    "ticket_id": int or None}. Pure and total."""
    try:
        if not isinstance(result, dict):
            return None
        args = args if isinstance(args, dict) else {}
        op = str(args.get("tool") or "").strip() if name == "operate" else name
        res = result.get("result")
        if name == "build" and isinstance(result.get("card"), dict):
            c = result["card"]
            tid = c.get("ticket_id")
            tid = int(tid) if isinstance(tid, int) and tid > 0 else None
            ref = (f"the approval card for phase {c.get('phase_number')} "
                   f"('{str(c.get('title') or '')[:60]}')" + (f", ticket #{tid}" if tid else ""))
            return {"ref": ref, "where": f"on {ref}", "ticket_id": tid}
        if result.get("status") != "ok" or not isinstance(res, dict):
            return None
        if name == "escalate":
            if res.get("status") != "open" or res.get("auto_resolved") is True:
                return None
            tid, src = int(res.get("ticket_id")), args
        elif op in RESULT_ASK_TOOLS and isinstance(res.get("ask"), dict):
            tid, src = int(res["ask"].get("ticket_id")), res["ask"]
        else:
            return None
        if tid <= 0 or not _card_options(src):
            return None
        return {"ref": f"ticket #{tid}", "where": f"in the question box (ticket #{tid})",
                "ticket_id": tid}
    except Exception:  # noqa: BLE001
        return None


# ── a repair's own words: never a box it did not fill ───────────────────────
# A k3 reply ended "Want me to put this on
# the build bench?", the guard fired, and the repair round ran NO tool and
# wrote "Since this is a real fork in the road, I've put it in the question box
# rather than leaving it as a chat question." The row's go_ahead.asked_in_box
# was false and the context ledger held no tool result: the person was told a
# card existed that never did. The correction names the question box, so the
# repair's words reach for it; whether one exists is a fact the turn holds
# (the cards open in front of the person, card_from_tool), so the turn checks
# it — session.py holds a repair's text until that round's calls have run and
# sends it through repair_text.
#
# A sentence refers to the box when it names it: the question box, an ask /
# decision card, the box or card above / below, something put in or into the
# box, escalating as the Host's own act, a decision ticket. A clause that
# negates it ("I haven't put it in the question box") claims nothing.
# Escalating counts only as the Host's act: the first person ("I've
# escalated"), the act on an object ("escalated it", "escalating the
# choice"), or a sentence that opens on it ("Escalated — pick there"). The
# ordinary word ("latency escalated", "the escalation policy") and server
# talk ("restarted it on the box", "deployed to the box") name no box, and
# with no card open a match drops the repair's whole text (repair_text), the
# work it reports included.
_BOX_CLAIM = re.compile(
    r"(?i)\bquestion[\s-]+box(?:es)?\b"
    r"|\b(?:ask|question|decision|choice|options?)\s+card\b"
    r"|\b(?:the|that|this|a)\s+(?:box|card)\s+(?:above|below)\b"
    r"|\b(?:in|into)\s+(?:the|a|your)\s+box\b"
    r"|\b(?:I(?:['’](?:ve|m|ll)|\s+(?:have|am|will))?|we(?:['’](?:ve|ll)|\s+(?:have|will))?)"
    r"\s+(?:just\s+|now\s+|already\s+|also\s+)?escalat(?:e|ed|es|ing)\b"
    r"|\bescalat(?:e|ed|es|ing)\s+(?:it|this|that|these|those|them"
    r"|the\s+(?:question|choice|decision|options?|pick|naming))\b"
    r"|^\s*escalat(?:ed|ing)\b"
    r"|\bdecision\s+ticket\b")
_UNIT_SPLIT = re.compile(r"((?<=[.!?…])\s+|\s*\n\s*)")
_CLAUSE_OF = re.compile(r"[,;:—–(]|\s-\s")
_NAMES_A_TICKET = re.compile(r"#\s?\d+")


def _claims_box(sentence: str) -> bool:
    m = _BOX_CLAIM.search(sentence or "")
    if not m:
        return False
    starts = [0] + [c.end() for c in _CLAUSE_OF.finditer(sentence) if c.end() <= m.start()]
    clause = sentence[starts[-1]:]
    nxt = _CLAUSE_OF.search(clause, m.start() - starts[-1])
    clause = clause[:nxt.start()] if nxt else clause
    return not _NEG.search(clause)


def _card_name(card: dict) -> str:
    tid = card.get("ticket_id")
    return f"ticket #{tid}" if isinstance(tid, int) and tid > 0 else str(card.get("ref") or "").strip()


def repair_text(text: str, card: dict | None, *, name_it: bool = True) -> tuple[str, list[str]]:
    """A go-ahead repair's own text, checked against what the turn holds:
    (the text to send, the sentences that claimed an ask never made).
    `card`: the card open in front of the person once the repair round's
    calls have run — one that round's `escalate` filed, or one already open
    (card_from_tool) — or None. `name_it`: the card was open before the
    round (one the round filed shows right beside the text).

    With no card, a sentence that refers to the question box claims an ask
    that was never made, and the repair's text is not sent at all: it was
    written as the follow-on to that ask ("I've put it in the question box —
    pick there and I'll start"), so no part of it can be trusted to stand
    alone. The draft above it is already on screen, intact, and the calls the
    round did run show as their own chips. With a card, the claim is true; for
    a card open from before the round, the first such sentence, when none
    names a ticket, is made to name it, so the pointer is to the card meant.
    Text that refers to no box comes back unchanged. Pure and total."""
    if not isinstance(text, str) or not text.strip():
        return (text if isinstance(text, str) else ""), []
    try:
        parts = _UNIT_SPLIT.split(text)
        units, seps = parts[0::2], parts[1::2]
        hits = [i for i, u in enumerate(units) if u.strip() and _claims_box(u)]
        if not hits:
            return text, []
        if not card:
            return "", [units[i].strip() for i in hits]
        if not name_it:
            return text, []
        name = _card_name(card)
        if not name or any(_NAMES_A_TICKET.search(units[j]) for j in hits):
            return text, []
        u = units[hits[0]]
        m = re.search(r"[.!?…]+[\"'”’)\]*_]*\s*$", u)
        cut = m.start() if m else len(u.rstrip())
        units[hits[0]] = u[:cut].rstrip() + f" ({name})" + u[cut:]
        out = units[0]
        for k in range(1, len(units)):
            out += seps[k - 1] + units[k]
        return out, []
    except Exception:  # noqa: BLE001 — a broken check never takes a turn down
        return text, []


def correction(text: str, phrase: str, *, sentence: str | None = None,
               close_turn_recorded: bool = False, in_handoff: bool = False,
               level: str = "explicit", open_card: dict | None = None,
               box_only: bool = False) -> str:
    """The user-role message injected for the one repair round. Same frame as
    the claim guard's: the draft is already on the user's screen, so the model
    continues from it instead of re-writing it.

    The detector is a high-recall PREFILTER (measured on fresh
    real text it catches ~97% of asks and also flags 8-16% of finished
    reports), so the Host makes the call on the quoted `sentence` — the one
    that fired. What it may answer follows the gate's TIERS:
      - a CORE or EXPLICIT form (`level`: a word to say or type, a direct
        question to the user, a reply request with the Host's step on it, the
        user's readiness gating the next step, input the Host needs) and a
        handoff that records a wait (`in_handoff`) are asks by their shape:
        the Host does the work or asks in the question box — nothing else;
      - a JUDGED reading (an offer, a decision handed over, a condition) may
        also be no ask at all, so a third answer is offered: end the turn
        adding nothing (session.py ends the turn on an empty continuation or a
        close_turn with no text, the reply intact, and records the outcome).
    Anything but "judged" — an unknown level included — gets two answers.

    `close_turn_recorded`: that close_turn's writes already ran; only its
    handoff is taken from the next one (session.py), because the handoff is
    what the next turn starts from and it must say where the work stands.
    `open_card`: a card this turn filed is already in front of the person
    (card_from_tool). The correction names it and never sends
    the Host to `escalate` again — a second ticket for the same question is
    what that would file. There is no tool that adds to an open decision
    ticket (tickets ops: public_tool_registry.py), so a different question
    waits for this one's answer, which comes back to the Host. It describes
    the ending's SHAPE rather than giving one to copy.

    `box_only`: an exit past the turn's loop (the forced text after the turn
    was closed or at the round backstop, the retry after an empty reply —
    session.py), where `escalate` is the one call that runs. Two
    answers only: the question box, or a rewrite that says what is done and
    what is left without asking."""
    seen = (text or "").strip()
    seen = seen if len(seen) <= 1500 else ("…" + seen[-1500:])
    rule = re.sub(r"\s*\([a-e]\)\s*$", "", str(phrase or "asks for a go-ahead"))
    fired = (sentence or "").strip()
    fired = fired if len(fired) <= 400 else (fired[:400] + "…")
    if open_card and not in_handoff:
        return (
            "[SYSTEM — do not mention this message to the user]\n"
            "You just wrote, and THE USER HAS ALREADY SEEN, this:\n\n"
            f"---\n{seen}\n---\n\n"
            f"It ends by asking the user:\n\n> {fired or seen[-300:]}\n\n"
            f"Your question is already {open_card.get('where') or 'on its card'}. "
            "End your reply without asking it again; say what you will do once they "
            "answer, naming the step — what their answer starts (the build, run or "
            "change), said as what happens, not as a condition on them. If you have "
            "a different question, ask it once they have answered this one: the "
            "answer comes back to you.\n\n"
            "That text is already on their screen and cannot be unsent, so whatever "
            "you write next is APPENDED to it. Do not repeat it — continue from it.\n\n"
            "Never end a turn asking for a go-ahead."
        )
    if str(phrase or "") == DEFER_RULE and not in_handoff:
        return _defer_correction(seen, fired, close_turn_recorded=close_turn_recorded,
                                 box_only=box_only)
    judged = level == "judged" and not in_handoff
    what = (f"Your close_turn handoff records the work as waiting on the user's "
            f"go:\n\n> {phrase}\n"
            if in_handoff else
            f"This sentence in it hands the next step back to the user (it {rule}):\n\n"
            f"> {fired or seen[-300:]}\n"
            if not judged else
            f"This sentence in it was flagged as handing the next step back to "
            f"the user (it {rule}):\n\n> {fired or seen[-300:]}\n")
    ct_line = ("\nYour close_turn is recorded. If you end with close_turn "
               "again, carry only the handoff — what YOU do next, not a wait for "
               "the user's go; only its handoff is kept.\n"
               if close_turn_recorded else "")
    third = ("3. If that sentence does not in fact ask the user for anything — it "
             "reports finished work, or describes, or points to where something is "
             "— end the turn now, adding nothing: reply with no text at all (or "
             "with close_turn alone).\n" if judged else "")
    if box_only:
        options = (
            "Now do one of these — `escalate` is the one call that runs now:\n"
            "1. If a real choice remains that only the user can make, call "
            "`escalate` with the options (each with its consequence). That is the "
            "question box: the user answers there, and their answer starts the "
            "work.\n"
            "2. Otherwise rewrite the ending: say what is done and what is left, as "
            "a statement, without asking whether to go on.\n")
    else:
        options = (
            "Now, in this turn, do one of these:\n"
            "1. DO THE WORK — make the calls it needs this round, then say what "
            "you started and its id.\n"
            "2. If a real choice remains that only the user can make, call "
            "`escalate` with the options (each with its consequence). That is the "
            "question box: the user answers there and the answer comes back into "
            "this turn.\n"
            + third)
    return (
        "[SYSTEM — do not mention this message to the user]\n"
        "You just wrote, and THE USER HAS ALREADY SEEN, this:\n\n"
        f"---\n{seen}\n---\n\n"
        f"{what}\n"
        + ("If it does ask that, it would end the turn" if judged else "That ends the turn")
        + ": the user has to come back and type one word, and that word starts a "
        "new turn from cold. The user already asked for this; their request is "
        "the go-ahead.\n\n"
        "That text is already on their screen and cannot be unsent, so "
        "whatever you write next is APPENDED to it. Do not repeat it — "
        "continue from it.\n\n"
        + options + "\n"
        "Where the platform itself asks the user — a phase with gate='user', "
        "a merge (Code Approval), a network or machine change — its card or "
        "button does the asking: say where it is, never ask for a word. If "
        "what you offered is extra work the user did not ask for, leave the "
        "offer out.\n"
        + ct_line +
        "Never end a turn asking for a go-ahead."
    )
