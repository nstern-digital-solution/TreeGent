"""Agent identity: random human-like name + personality traits, assigned at
actor creation (R49). Lives on the actor document so every surface (runtime
system prompt, mail address, web roster) reads the same source of truth.

Names are stored in a wordlist file (data), not code — extend freely.
"""
import random

# Small curated starter set (first+last). Extendable via the DB later if
# operators want locale control; v1 ships a fixed international mix.
FIRST = [
    "Ada", "Bruno", "Clara", "Dario", "Elin", "Farid", "Greta", "Hugo",
    "Ines", "Jonas", "Klara", "Leon", "Mira", "Nils", "Olga", "Pavel",
    "Rosa", "Samir", "Tessa", "Ulf", "Vera", "Willem", "Yara", "Zeno",
    "Alma", "Bo", "Cato", "Elio", "Freya", "Gil", "Heike", "Ivo",
]
LAST = [
    "Novak", "Berger", "Costa", "Dalton", "Eriksen", "Faber", "Galan",
    "Hartmann", "Ivarsson", "Jovic", "Keller", "Lindqvist", "Marchetti",
    "Norlin", "Okafor", "Peralta", "Quint", "Renard", "Silva", "Thaler",
    "Ustinov", "Voss", "Weiss", "Xhoni", "Yilmaz", "Zahler", "Berg",
    "Costa", "Dorn", "Falk", "Gerber", "Holt",
]

# Trait pools — pick 2-3 per agent, phrased as prompt-usable adjectives.
TRAITS = [
    "precise and methodical", "brisk and pragmatic", "warm and encouraging",
    "skeptical and thorough", "curious and exploratory", "calm under pressure",
    "direct, no small talk", "enthusiastic about hard problems",
    "patient with beginners", "competitive about deadlines",
    "diplomatically careful", "plain-spoken and honest",
    "optimistic by default", "detail-obsessed", "big-picture oriented",
    "quietly witty", "formally polite", "allergic to vagueness",
    "allergic to hype", "protective of coworkers' time",
    "happy to admit uncertainty", "stubborn about correctness",
]

# Work styles folded into the persona line.
STYLE = [
    "writes short sentences", "prefers bullet lists", "thinks out loud",
    "asks clarifying questions first", "proposes options with tradeoffs",
    "documents as they go", "tests before claiming done",
    "escalates early rather than guessing",
]


def assign_identity() -> dict:
    """Random identity block for a new agent actor (R49)."""
    first, last = random.choice(FIRST), random.choice(LAST)
    # distinct first+last draw
    while first == last:
        first = random.choice(FIRST)
    traits = random.sample(TRAITS, k=3)
    style = random.choice(STYLE)
    return {
        "persona_name": f"{first} {last}",
        "persona_traits": traits,
        "persona_style": style,
    }


def persona_line(actor: dict) -> str:
    """One prompt-ready paragraph describing the agent's persona."""
    p = actor.get("persona") or {}
    name = p.get("persona_name") or actor.get("display_name") or "agent"
    traits = ", ".join(p.get("persona_traits") or [])
    style = p.get("persona_style") or ""
    bits = [f"You are {name}"]
    if traits:
        bits.append(f"Your working personality: {traits}")
    if style:
        # pool verbs are 3rd-person ("writes...", "asks...") — strip the -s
        # for the "you ..." phrasing
        you = style
        for v3, v1 in [("writes", "write"), ("prefers", "prefer"),
                       ("thinks", "think"), ("asks", "ask"),
                       ("proposes", "propose"), ("documents", "document"),
                       ("tests", "test"), ("escalates", "escalate")]:
            you = you.replace(v3, v1, 1)
        bits.append(f"Your style: you {you}")
    return ". ".join(bits) + "."
