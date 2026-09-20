/*
 * Gauntlet bridge for Forge. GPLv3-or-later, see Bridge.java.
 */
package forge.gauntlet;

import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import forge.game.Game;
import forge.game.card.Card;
import forge.game.GameEntity;
import forge.game.card.CardCollectionView;
import forge.game.combat.Combat;
import forge.game.phase.PhaseHandler;
import forge.game.player.Player;
import forge.game.zone.ZoneType;

/**
 * Renders a game into the compact JSON a seat reasons over.
 *
 * Two properties matter more than completeness here.
 *
 * Size, because every byte is paid for on every round trip. Cards appear as
 * short slugs and their oracle text is sent exactly once per game per seat, the
 * first time that seat could see the card. A seat already told what Cultivate
 * does is never told again.
 *
 * Stability, because two transcripts of the same matchup should diff cleanly.
 * Field order is fixed and zones are emitted in a deterministic order rather
 * than whatever order Forge happens to hold them in.
 */
final class StateView {

    /** Slugs this seat has already been shown the oracle text for. */
    private final Set<String> known = new HashSet<>();

    /** Oracle text gathered while building the current request. */
    private JsonObject pendingCards = new JsonObject();

    static String slug(String name) {
        StringBuilder sb = new StringBuilder(name.length());
        boolean lastWasDash = true; // suppresses a leading dash
        for (int i = 0; i < name.length(); i++) {
            char c = Character.toLowerCase(name.charAt(i));
            if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) {
                sb.append(c);
                lastWasDash = false;
            } else if (!lastWasDash) {
                sb.append('-');
                lastWasDash = true;
            }
        }
        // Trailing dash from punctuation at the end of a name.
        if (sb.length() > 0 && sb.charAt(sb.length() - 1) == '-') {
            sb.setLength(sb.length() - 1);
        }
        return sb.toString();
    }

    /**
     * Takes the oracle text accumulated during the last state render and clears
     * it. Call once per request, after {@link #state}.
     */
    JsonObject drainNewCards() {
        JsonObject out = pendingCards;
        pendingCards = new JsonObject();
        return out;
    }

    /**
     * Forge keeps bookkeeping cards in the command zone alongside the real
     * commander - the commander tax effect, emblems it models as cards. They
     * are not objects a player acts on, and showing them invites a seat to
     * reason about something that is not there.
     */
    private static boolean isBookkeeping(Card c) {
        String n = c.getName();
        return n == null || n.isEmpty() || n.endsWith("Effect");
    }

    /** Registers a card as seen and queues its text if this is the first time. */
    private String ref(Card c) {
        String name = c.isFaceDown() ? "Face-down card" : c.getName();
        String s = slug(name);
        if (!c.isFaceDown() && known.add(s)) {
            JsonObject detail = new JsonObject();
            detail.addProperty("name", name);

            // A land's mana cost stringifies to "no cost", which reads as a
            // missing value rather than the absence of one. Leave it out.
            String mana = c.getManaCost() == null ? "" : c.getManaCost().toString();
            if (!mana.isEmpty() && !"no cost".equalsIgnoreCase(mana)) {
                detail.addProperty("cost", mana);
            }

            detail.addProperty("type", c.getType().toString());

            // Printed stats, not current ones. The battlefield entry carries
            // what the creature is right now, this is what the card says.
            if (c.isCreature()) {
                detail.addProperty("pt", c.getBasePower() + "/" + c.getBaseToughness());
            }

            String oracle = c.getOracleText();
            if (oracle != null && !oracle.isEmpty()) {
                // Forge stores rules text with literal backslash-n between
                // paragraphs. Left alone it reaches the seat as the two
                // characters and makes every multi-line card unreadable.
                detail.addProperty("text", oracle.replace("\\n", "\n").replace('\r', '\n'));
            }
            pendingCards.add(s, detail);
        }
        return s;
    }

    /** A card on the battlefield, with only the state that changes play. */
    private JsonObject permanent(Card c) {
        JsonObject o = new JsonObject();
        o.addProperty("c", ref(c));
        if (c.isCreature()) {
            o.addProperty("pt", c.getNetPower() + "/" + c.getNetToughness());
            if (c.getDamage() > 0) {
                o.addProperty("dmg", c.getDamage());
            }
            if (c.isSick()) {
                o.addProperty("sick", true);
            }
        }
        if (c.isTapped()) {
            o.addProperty("tapped", true);
        }
        return o;
    }

    private JsonArray permanents(CardCollectionView cards) {
        JsonArray a = new JsonArray();
        for (Card c : cards) {
            if (isBookkeeping(c)) {
                continue;
            }
            a.add(permanent(c));
        }
        return a;
    }

    private JsonArray refs(CardCollectionView cards) {
        JsonArray a = new JsonArray();
        for (Card c : cards) {
            if (isBookkeeping(c)) {
                continue;
            }
            a.add(ref(c));
        }
        return a;
    }

    /**
     * The full state block for one seat's point of view.
     *
     * Hidden information stays hidden: an opponent's hand and library are
     * counts, never contents. A seat that could cheat would make every
     * transcript worthless as evidence about a deck.
     */
    JsonObject state(Game game, Player me) {
        PhaseHandler ph = game.getPhaseHandler();
        JsonObject o = new JsonObject();
        o.addProperty("turn", ph.getTurn());
        o.addProperty("phase", ph.getPhase() == null ? "?" : ph.getPhase().name().toLowerCase(Locale.ROOT));
        o.addProperty("active", ph.getPlayerTurn() == null ? "?" : ph.getPlayerTurn().getName());
        o.addProperty("you", me.getName());

        JsonObject you = new JsonObject();
        you.addProperty("life", me.getLife());
        you.add("hand", refs(me.getCardsIn(ZoneType.Hand)));
        you.add("battlefield", permanents(me.getCardsIn(ZoneType.Battlefield)));
        you.add("graveyard", refs(me.getCardsIn(ZoneType.Graveyard)));
        you.add("command", refs(me.getCardsIn(ZoneType.Command)));
        you.add("exile", refs(me.getCardsIn(ZoneType.Exile)));
        you.addProperty("library", me.getCardsIn(ZoneType.Library).size());
        o.add("me", you);

        JsonArray opps = new JsonArray();
        for (Player p : game.getPlayers()) {
            if (p == me) {
                continue;
            }
            JsonObject op = new JsonObject();
            op.addProperty("name", p.getName());
            op.addProperty("life", p.getLife());
            op.addProperty("hand_size", p.getCardsIn(ZoneType.Hand).size());
            op.addProperty("library", p.getCardsIn(ZoneType.Library).size());
            op.add("battlefield", permanents(p.getCardsIn(ZoneType.Battlefield)));
            op.add("graveyard", refs(p.getCardsIn(ZoneType.Graveyard)));
            op.add("command", refs(p.getCardsIn(ZoneType.Command)));
            JsonObject dmg = new JsonObject();
            for (Card cmd : p.getCommanders()) {
                int d = me.getCommanderDamage(cmd);
                if (d > 0) {
                    dmg.addProperty(slug(cmd.getName()), d);
                }
            }
            if (dmg.size() > 0) {
                op.add("cmd_damage_to_me", dmg);
            }
            opps.add(op);
        }
        o.add("opponents", opps);

        JsonArray stack = new JsonArray();
        game.getStack().forEach(si -> stack.add(si.getSpellAbility().getStackDescription()));
        if (stack.size() > 0) {
            o.add("stack", stack);
        }

        JsonArray combat = combat(game);
        if (combat.size() > 0) {
            o.add("combat", combat);
        }

        return o;
    }

    /**
     * Who is attacking whom, and what is already blocking.
     *
     * Without this a seat asked to declare blockers is looking at a board where
     * the only clue is which creatures happen to be tapped, which does not say
     * what they are attacking and does not cover vigilance at all. Blocking
     * blind is not a judgment call, it is a coin flip.
     */
    private JsonArray combat(Game game) {
        JsonArray out = new JsonArray();
        Combat c = game.getCombat();
        if (c == null) {
            return out;
        }
        for (Card attacker : c.getAttackers()) {
            JsonObject entry = new JsonObject();
            entry.addProperty("c", ref(attacker));
            if (attacker.isCreature()) {
                entry.addProperty("pt", attacker.getNetPower() + "/" + attacker.getNetToughness());
            }
            GameEntity defender = c.getDefenderByAttacker(attacker);
            if (defender != null) {
                entry.addProperty("attacking", defender.toString());
            }
            JsonArray blockers = new JsonArray();
            for (Card blocker : c.getBlockers(attacker)) {
                blockers.add(ref(blocker));
            }
            if (blockers.size() > 0) {
                entry.add("blocked_by", blockers);
            }
            out.add(entry);
        }
        return out;
    }
}
