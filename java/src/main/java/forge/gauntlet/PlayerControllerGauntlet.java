/*
 * Gauntlet bridge for Forge. GPLv3-or-later, see Bridge.java.
 */
package forge.gauntlet;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Set;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import forge.LobbyPlayer;
import forge.ai.ComputerUtilAbility;
import forge.ai.ComputerUtilCost;
import forge.ai.PlayerControllerAi;
import forge.game.Game;
import forge.game.GameLogEntry;
import forge.game.card.Card;
import forge.game.card.CardCollection;
import forge.game.combat.Combat;
import forge.game.player.Player;
import forge.game.spellability.SpellAbility;
import forge.game.zone.ZoneType;

/**
 * A Forge player controller whose interesting decisions are made elsewhere.
 *
 * It extends {@link PlayerControllerAi} rather than the bare
 * {@code PlayerController} on purpose. That base class has 110 abstract methods
 * covering every question Magic can ask, most of which have one sane answer and
 * no bearing on whether a deck is good. Inheriting the AI means the ~15 that
 * carry real judgment can be routed outward while the rest stay fast, correct
 * and inside the JVM.
 *
 * Which of those 15 actually leave is configurable. A decision kind that is not
 * enabled falls straight through to {@code super}, so throughput can be traded
 * against fidelity without touching this file.
 *
 * Every override follows the same shape, and new ones should too:
 *
 *   1. bail to super if this kind is not routed, or the choice is forced
 *   2. build the option list, and remember the mapping back to Forge objects
 *   3. ask, with a timeout
 *   4. on any doubt at all, fall back to super
 *
 * Step 4 is not optional. A seat that hangs, crashes or answers nonsense must
 * cost the run a slightly worse decision, never a lost game.
 */
public class PlayerControllerGauntlet extends PlayerControllerAi {

    private final Bridge bridge;
    private final String seat;
    private final Set<String> routed;
    private final StateView view = new StateView();

    public PlayerControllerGauntlet(Game game, Player p, LobbyPlayer lp, Bridge bridge, String seat,
            Set<String> routedKinds) {
        super(game, p, lp);
        this.bridge = bridge;
        this.seat = seat;
        this.routed = routedKinds;
    }

    /**
     * Set GAUNTLET_TRACE=1 to see every decision point the controller reaches,
     * routed or not. The question this answers is "is Forge even asking me
     * this?", which is otherwise invisible and was worth an hour the first time.
     */
    private static final boolean TRACE = "1".equals(System.getenv("GAUNTLET_TRACE"));

    private void trace(String kind, boolean willRoute) {
        if (TRACE) {
            System.err.println("[gauntlet] " + seat + " " + kind + (willRoute ? " -> seat" : " -> forge"));
        }
    }

    private boolean routes(String kind) {
        boolean yes = bridge != null && !bridge.isBroken() && routed.contains(kind);
        trace(kind, yes);
        return yes;
    }

    /**
     * Builds the common envelope. Callers add {@code options} and any payload.
     */
    private JsonObject envelope(String kind, String prompt) {
        JsonObject req = new JsonObject();
        req.addProperty("seat", seat);
        req.addProperty("kind", kind);
        req.addProperty("prompt", prompt);
        req.add("state", view.state(getGame(), getPlayer()));
        JsonObject fresh = view.drainNewCards();
        if (fresh.size() > 0) {
            req.add("new_cards", fresh);
        }
        JsonArray since = logSinceLastAsk();
        if (since.size() > 0) {
            req.add("since", since);
        }
        return req;
    }

    /** How many game log entries this seat has already been shown. */
    private int logCursor = 0;

    /**
     * What has happened since this seat was last asked anything.
     *
     * Without it a seat casts a removal spell, gets no acknowledgement, and
     * finds out only by comparing hand sizes across two prompts that the spell
     * left its hand, spent its mana and killed nothing. Forge chooses targets,
     * so a spell the seat picked can resolve against something it did not
     * expect, or fizzle for want of a legal target, and neither shows up
     * anywhere in the board state.
     *
     * The game log is Forge's own account of events, so it covers that, the
     * opponent's turn, triggers, damage and everything else the seat missed
     * while it was not being asked.
     */
    private JsonArray logSinceLastAsk() {
        JsonArray out = new JsonArray();
        List<GameLogEntry> all = getGame().getGameLog().getAllEntries();
        // Forge appends, so anything past the cursor is new. A log that somehow
        // shrank means a new game in the same match, so start over rather than
        // index off the end.
        if (all.size() < logCursor) {
            logCursor = 0;
        }
        for (int i = logCursor; i < all.size(); i++) {
            String message = all.get(i).message();
            if (message != null && !message.isBlank()) {
                out.add(message.replace('\n', ' ').trim());
            }
        }
        logCursor = all.size();

        // A seat that has not been asked for several turns can accumulate a
        // very long tail. The recent end is the part that bears on the decision
        // in front of it.
        int limit = 40;
        if (out.size() <= limit) {
            return out;
        }
        JsonArray trimmed = new JsonArray();
        trimmed.add("... " + (out.size() - limit) + " earlier events omitted");
        for (int i = out.size() - limit; i < out.size(); i++) {
            trimmed.add(out.get(i));
        }
        return trimmed;
    }

    /**
     * Sends a request whose answer is one index into {@code optionCount}.
     *
     * @return the chosen index, or -1 to mean "you decide, Forge".
     */
    private int askForIndex(JsonObject req, int optionCount) {
        JsonObject reply = bridge.ask(req);
        if (reply == null || !reply.has("choice") || reply.get("choice").isJsonNull()) {
            return -1;
        }
        int choice;
        try {
            choice = reply.get("choice").getAsInt();
        } catch (RuntimeException e) {
            return -1;
        }
        // An out-of-range answer is a bug on the far side, not something to
        // propagate into the game state.
        return (choice >= 0 && choice < optionCount) ? choice : -1;
    }

    /** How much of an ability's description an option label carries. */
    private static final int LABEL_LIMIT = 90;

    /**
     * Trims an option label to something a seat can scan.
     *
     * Forge's description of a spell is its full rules text, so an option list
     * of five spells is five paragraphs. The seat already has the oracle text
     * for every card it can see, sent once, so repeating it in the label buys
     * nothing and buries the choice. Cut on a word boundary, and never cut the
     * card name, which is the part that identifies the option.
     */
    private static String shorten(String label) {
        String flat = label.replace('\n', ' ').replace('\r', ' ').replaceAll("\\s+", " ").trim();
        if (flat.length() <= LABEL_LIMIT) {
            return flat;
        }
        int cut = flat.lastIndexOf(' ', LABEL_LIMIT);
        if (cut < LABEL_LIMIT / 2) {
            cut = LABEL_LIMIT;
        }
        return flat.substring(0, cut) + "...";
    }

    private static JsonObject option(int i, String label) {
        JsonObject o = new JsonObject();
        o.addProperty("i", i);
        o.addProperty("label", label);
        return o;
    }

    // ---------------------------------------------------------------- casting

    @Override
    public List<SpellAbility> chooseSpellAbilityToPlay() {
        if (!routes("cast_or_pass")) {
            return super.chooseSpellAbilityToPlay();
        }

        // Forge hands back the plan it would follow. Asking the seat is only
        // worth a round trip when there is more than one thing it could do.
        List<SpellAbility> candidates = getPlayableSpellAbilities();
        if (candidates.isEmpty()) {
            return super.chooseSpellAbilityToPlay();
        }

        // Forge hands priority back after every resolution, so the same question
        // arrives many times in one step with nothing changed. A seat that
        // already passed on exactly this board, in exactly this step, with
        // exactly these options, has answered it. Asking again wastes a round
        // trip and buries the decisions that matter in noise.
        String fingerprint = passFingerprint(candidates);
        if (fingerprint.equals(lastPassed)) {
            return null;
        }

        // Three cheap outs before paying for a round trip. Measured over twelve
        // agent games, 65% of cast_or_pass decisions were the seat choosing to
        // pass and 32% offered a single alternative, so most of the spend was
        // on questions with one sensible answer.
        if (notWorthAsking(candidates)) {
            lastPassed = fingerprint;
            return super.chooseSpellAbilityToPlay();
        }

        JsonObject req = envelope("cast_or_pass",
                "You have priority. Choose something to play, or pass.");
        JsonArray opts = new JsonArray();
        opts.add(option(0, "Pass priority"));
        for (int i = 0; i < candidates.size(); i++) {
            SpellAbility sa = candidates.get(i);
            Card host = sa.getHostCard();

            // Forge's own label is the ability, not the card. Two lands in hand
            // both come back as "Play land", which is not a choice anyone can
            // make. Lead with the card name whenever it is not already there.
            String label = sa.toUnsuppressedString();
            if (host != null && !label.contains(host.getName())) {
                label = host.getName() + " - " + label;
            }
            label = shorten(label);

            JsonObject o = option(i + 1, label);
            if (host != null) {
                o.addProperty("card", StateView.slug(host.getName()));
            }
            // "no cost" is Forge's string for a free ability. It reads as if
            // something were missing, so leave the field out instead.
            if (sa.getPayCosts() != null) {
                String cost = sa.getPayCosts().toString();
                // Forge writes costs with a CARDNAME placeholder it substitutes
                // at display time. Left in, a seat reads "Sacrifice CARDNAME".
                if (host != null) {
                    cost = cost.replace("CARDNAME", host.getName());
                }
                if (!cost.isEmpty() && !"no cost".equalsIgnoreCase(cost)) {
                    o.addProperty("cost", cost);
                }
            }
            opts.add(o);
        }
        req.add("options", opts);

        int choice = askForIndex(req, opts.size());
        if (choice < 0) {
            return super.chooseSpellAbilityToPlay();
        }
        if (choice == 0) {
            lastPassed = fingerprint;
            return null; // pass priority
        }
        // Anything played changes the board, so the previous pass no longer
        // describes a situation the seat has already judged.
        lastPassed = null;
        List<SpellAbility> chosen = new ArrayList<>(1);
        chosen.add(candidates.get(choice - 1));
        return chosen;
    }

    /**
     * Whether this decision is too one-sided to be worth a seat's attention.
     *
     * Each of these was measured rather than guessed, and each hands the
     * decision to Forge's AI rather than inventing an answer, so the worst case
     * is the play Forge would have made anyway.
     */
    private boolean notWorthAsking(List<SpellAbility> candidates) {
        // One land and nothing else. Holding a land back is a real play in rare
        // spots, and Forge's AI already models the main-phase-two version of it.
        if (candidates.size() == 1 && candidates.get(0).isLandAbility()) {
            return true;
        }

        // Someone else's turn and no untapped mana. Whatever is on offer is a
        // free ability, and a seat that wanted one had its chance on its own
        // turn.
        Player p = getPlayer();
        if (!p.equals(getGame().getPhaseHandler().getPlayerTurn()) && untappedSources(p) == 0) {
            return true;
        }

        return false;
    }

    /** Lands and mana rocks this player could still tap. */
    private int untappedSources(Player p) {
        int n = 0;
        for (Card c : p.getCardsIn(ZoneType.Battlefield)) {
            if (!c.isTapped() && !c.getManaAbilities().isEmpty()) {
                n++;
            }
        }
        return n;
    }

    /** The board and options the seat last chose to pass on, or null. */
    private String lastPassed = null;

    /**
     * Identifies "this exact question, on this exact board, in this step".
     *
     * Deliberately includes the opponents' life and board size and the stack
     * depth as well as the seat's own options. A pass is only safely repeatable
     * while nothing a seat might have wanted to respond to has changed.
     */
    private String passFingerprint(List<SpellAbility> candidates) {
        Game game = getGame();
        StringBuilder sb = new StringBuilder();
        sb.append(game.getPhaseHandler().getTurn()).append('|');
        // Phase is deliberately not part of this. Forge offers priority in every
        // step, so including it asked the seat the same question six times a
        // turn - upkeep, draw, main, three combat steps - with an identical
        // board and identical options each time. That was most of a 224
        // decision game.
        //
        // Leaving it out is safe because sorcery-speed plays only appear in the
        // option list during a main phase, so arriving at one changes the
        // options and the seat gets asked again on its own.
        sb.append(game.getPhaseHandler().getPlayerTurn()).append('|');
        sb.append(game.getStack().size()).append('|');
        for (Player p : game.getPlayers()) {
            sb.append(p.getName()).append(':').append(p.getLife()).append(':')
              .append(p.getCardsIn(ZoneType.Battlefield).size()).append(':')
              .append(p.getCardsIn(ZoneType.Hand).size()).append('|');
        }
        List<String> labels = new ArrayList<>(candidates.size());
        for (SpellAbility sa : candidates) {
            labels.add(sa.toUnsuppressedString());
        }
        Collections.sort(labels); // Forge's enumeration order is not stable
        sb.append(labels);
        return sb.toString();
    }

    /**
     * Everything this player could legally start playing right now.
     *
     * The filter here is legality, not the AI's opinion. Forge's AI keeps its
     * own much narrower list of abilities it would actually choose, and offering
     * only those would reduce the seat to picking among the AI's ideas. The
     * whole point is to let a seat make a play the AI would never consider.
     *
     * What the seat does not control is how the chosen spell gets cast.
     * Targeting, modes and mana payment are still Forge's AI, via
     * {@code playChosenSpellAbility}. Routing those outward is a later step, and
     * a separate decision kind.
     */
    private List<SpellAbility> getPlayableSpellAbilities() {
        Game game = getGame();
        Player p = getPlayer();
        List<SpellAbility> out = new ArrayList<>();

        // Spells and activated abilities from every zone the player can act from.
        CardCollection sources = ComputerUtilAbility.getAvailableCards(game, p);
        for (SpellAbility sa : ComputerUtilAbility.getSpellAbilities(sources, p)) {
            if (sa.isLandAbility()) {
                continue; // land drops are enumerated separately, below
            }
            // Mana abilities are never a decision. Tapping a land is something
            // you do to pay for a spell, and Forge's payment machinery does it
            // when the time comes. Offering them made a seat's option list
            // mostly "tap this land" and tempted it into floating mana with
            // nothing to spend it on.
            if (sa.isManaAbility()) {
                continue;
            }
            sa.setActivatingPlayer(p);
            if (sa.canPlay(false) && ComputerUtilCost.canPayCost(sa, p, sa.isTrigger())) {
                out.add(sa);
            }
        }

        // Land drops. getAvailableLandsToPlay already accounts for the land drop
        // having been used this turn, so anything it returns is a legal play.
        CardCollection lands = ComputerUtilAbility.getAvailableLandsToPlay(game, p);
        if (lands != null) {
            for (Card land : lands) {
                for (SpellAbility sa : land.getAllPossibleAbilities(p, true)) {
                    if (sa.isLandAbility()) {
                        out.add(sa);
                    }
                }
            }
        }
        return out;
    }

    // --------------------------------------------------------------- mulligan

    @Override
    public boolean mulliganKeepHand(Player firstPlayer, int cardsToReturn) {
        if (!routes("mulligan")) {
            return super.mulliganKeepHand(firstPlayer, cardsToReturn);
        }
        JsonObject req = envelope("mulligan",
                "Opening hand. Keep, or mulligan? You would put " + cardsToReturn
                        + " card(s) on the bottom if you keep.");
        JsonArray opts = new JsonArray();
        opts.add(option(0, "Mulligan"));
        opts.add(option(1, "Keep"));
        req.add("options", opts);

        int choice = askForIndex(req, 2);
        if (choice < 0) {
            return super.mulliganKeepHand(firstPlayer, cardsToReturn);
        }
        return choice == 1;
    }

    // ----------------------------------------------------------------- combat

    @Override
    public void declareAttackers(Player attacker, Combat combat) {
        if (!routes("attack")) {
            super.declareAttackers(attacker, combat);
            return;
        }
        // Let Forge work out the legal attack first. It knows about
        // restrictions, requirements and propaganda costs that the seat should
        // not have to reason about, and its answer is the fallback anyway.
        super.declareAttackers(attacker, combat);

        JsonObject req = envelope("attack", "Declare attackers.");
        JsonArray opts = new JsonArray();
        opts.add(option(0, "Attack as Forge proposes"));
        opts.add(option(1, "Do not attack"));
        req.add("options", opts);
        JsonArray proposed = new JsonArray();
        for (Card c : combat.getAttackers()) {
            proposed.add(c.getName());
        }
        req.add("proposed", proposed.size() == 0 ? proposalNone() : proposed);

        int choice = askForIndex(req, 2);
        if (choice == 1) {
            combat.clearAttackers();
        }
    }

    @Override
    public void declareBlockers(Player defender, Combat combat) {
        if (!routes("block")) {
            super.declareBlockers(defender, combat);
            return;
        }
        super.declareBlockers(defender, combat);

        JsonObject req = envelope("block", "Declare blockers.");
        JsonArray opts = new JsonArray();
        JsonArray blocks = describeBlocks(combat);
        // Say "no blocks" out loud. An empty list renders as nothing at all,
        // which leaves a seat choosing between "as Forge proposes" and "do not
        // block" without being told those are the same thing this time.
        opts.add(option(0, blocks.size() == 0
                ? "Block as Forge proposes (it proposes no blocks)"
                : "Block as Forge proposes"));
        opts.add(option(1, "Do not block"));
        req.add("options", opts);
        req.add("proposed", blocks.size() == 0 ? proposalNone() : blocks);

        int choice = askForIndex(req, 2);
        if (choice == 1) {
            for (Card blocker : new ArrayList<>(combat.getAllBlockers())) {
                combat.removeFromCombat(blocker);
            }
        }
    }

    /** An explicit empty proposal, so "nothing" is visible rather than absent. */
    private static JsonArray proposalNone() {
        JsonArray a = new JsonArray();
        a.add("nothing");
        return a;
    }

    private JsonArray describeBlocks(Combat combat) {
        JsonArray a = new JsonArray();
        for (Card attacker : combat.getAttackers()) {
            for (Card blocker : combat.getBlockers(attacker)) {
                a.add(blocker.getName() + " blocks " + attacker.getName());
            }
        }
        return a;
    }

    // ------------------------------------------------------------------ hooks

}
