/*
 * Gauntlet bridge for Forge. GPLv3-or-later, see Bridge.java.
 */
package forge.gauntlet;

import java.io.File;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.EnumSet;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

import com.google.gson.JsonObject;

import forge.GuiDesktop;
import forge.deck.Deck;
import forge.deck.io.DeckSerializer;
import forge.game.Game;
import forge.game.GameEndReason;
import forge.game.GameRules;
import forge.game.GameType;
import forge.game.Match;
import forge.game.player.RegisteredPlayer;
import forge.gui.GuiBase;
import forge.model.FModel;
import forge.player.GamePlayerUtil;
import forge.util.MyRandom;

/**
 * Entry point for a bridged game.
 *
 * Deliberately a sibling of Forge's own {@code SimulateMatch} rather than a
 * patch to it. The only thing that differs is which lobby player gets seated,
 * so the rest stays close enough to upstream to re-check against it when Forge
 * moves.
 *
 * <pre>
 *   java -cp forge.jar:gson.jar:gauntlet.jar forge.gauntlet.GauntletMain \
 *        --deck A=/path/a.dck --deck B=/path/b.dck \
 *        --bridge A=127.0.0.1:9001 \
 *        --format Commander --seed 12345 --timeout 300
 * </pre>
 *
 * A seat with no {@code --bridge} is played by Forge's AI. That is the control
 * arm for any experiment and the fastest way to run a deck a few thousand times.
 */
public final class GauntletMain {

    /** Decision kinds routed outward when a seat does not say otherwise. */
    private static final Set<String> DEFAULT_ROUTED =
            new HashSet<>(Arrays.asList("cast_or_pass", "mulligan", "attack", "block"));

    public static void main(String[] args) {
        bootstrapForge();

        Map<String, String> decks = new HashMap<>();
        Map<String, String> bridges = new HashMap<>();
        Map<String, Set<String>> routed = new HashMap<>();
        // Kept as a string until after the bootstrap: touching GameType loads
        // localised names, and that needs the GUI interface to exist first.
        String formatName = "Commander";
        Long seed = null;
        int games = 1;
        int decisionTimeoutMs = 300_000;
        int gameTimeoutSec = 900;

        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            switch (a) {
                case "--deck":
                    putPair(decks, args[++i], "--deck");
                    break;
                case "--bridge":
                    putPair(bridges, args[++i], "--bridge");
                    break;
                case "--routed": {
                    // --routed A=cast_or_pass,attack   (omit to use the default set)
                    String[] kv = split(args[++i], "--routed");
                    routed.put(kv[0], new HashSet<>(Arrays.asList(kv[1].split(","))));
                    break;
                }
                case "--format":
                    formatName = args[++i];
                    break;
                case "--seed":
                    seed = Long.parseLong(args[++i]);
                    break;
                case "--games":
                    games = Integer.parseInt(args[++i]);
                    break;
                case "--decision-timeout":
                    decisionTimeoutMs = Integer.parseInt(args[++i]) * 1000;
                    break;
                case "--game-timeout":
                    gameTimeoutSec = Integer.parseInt(args[++i]);
                    break;
                default:
                    System.err.println("unknown argument: " + a);
                    System.exit(2);
            }
        }

        if (decks.size() < 2) {
            System.err.println("need at least two --deck SEAT=path arguments");
            System.exit(2);
        }

        FModel.initialize(null, null);
        if (seed != null) {
            MyRandom.setRandom(new Random(seed));
        }

        GameType format = GameType.valueOf(formatName);
        GameRules rules = new GameRules(format);
        rules.setAppliedVariants(EnumSet.of(format));
        rules.setSimTimeout(gameTimeoutSec);

        List<String> seats = new ArrayList<>(decks.keySet());
        Collections.sort(seats); // seat order must not depend on map iteration order

        List<Bridge> open = new ArrayList<>();
        List<RegisteredPlayer> players = new ArrayList<>();

        try {
            for (String seat : seats) {
                Deck deck = DeckSerializer.fromFile(new File(decks.get(seat)));
                if (deck == null) {
                    System.err.println("could not load deck for seat " + seat + ": " + decks.get(seat));
                    System.exit(3);
                }

                RegisteredPlayer rp = format == GameType.Commander
                        ? RegisteredPlayer.forCommander(deck)
                        : new RegisteredPlayer(deck);

                String endpoint = bridges.get(seat);
                if (endpoint == null) {
                    rp.setPlayer(GamePlayerUtil.createAiPlayer(seat, seats.indexOf(seat)));
                } else {
                    Bridge b = Bridge.connect(endpoint, decisionTimeoutMs);
                    open.add(b);
                    Set<String> kinds = routed.getOrDefault(seat, DEFAULT_ROUTED);
                    rp.setPlayer(new LobbyPlayerGauntlet(seat, seat, b, kinds, null));
                }
                players.add(rp);
            }

            Match match = new Match(rules, players, "Gauntlet");
            for (int g = 0; g < games; g++) {
                runOneGame(match, g, open);
            }
        } catch (Exception e) {
            e.printStackTrace();
            System.exit(1);
        } finally {
            for (Bridge b : open) {
                b.close();
            }
        }
        System.out.flush();
    }

    /**
     * The minimum of Forge's desktop startup that a headless run needs.
     *
     * Forge's own {@code Main} does this before dispatching to {@code sim}, and
     * skipping it fails late and obscurely: {@link GameType}'s static
     * initialiser reaches for localised names and finds no resource bundle.
     * Must run before any Forge game class is touched.
     */
    private static void bootstrapForge() {
        // Forge's own workaround for a comparator that violates its contract.
        // Without it, sorting spell abilities can throw mid-game.
        System.setProperty("java.util.Arrays.useLegacyMergeSort", "true");

        // Order matters and is not obvious. GuiDesktop's static initialiser
        // reads the default screen device to work out UI scaling, so forcing
        // java.awt.headless before this line makes it throw HeadlessException.
        // Forge's own Main has the same ordering. Set headless afterwards,
        // where it stops anything later from trying to open a window.
        GuiBase.setInterface(new GuiDesktop());
        System.setProperty("java.awt.headless", "true");
    }

    private static void runOneGame(Match match, int index, List<Bridge> bridges) {
        long started = System.currentTimeMillis();
        Game game = match.createGame();
        try {
            match.startGame(game);
        } catch (Exception | StackOverflowError e) {
            // A crash mid-game is a Forge bug or a bridge bug. Either way the
            // run should record it and move on rather than take down the batch.
            e.printStackTrace();
            game.setGameOver(GameEndReason.Draw);
        }

        JsonObject result = new JsonObject();
        result.addProperty("kind", "game_result");
        result.addProperty("game", index + 1);
        result.addProperty("ms", System.currentTimeMillis() - started);
        result.addProperty("turns", game.getPhaseHandler().getTurn());
        if (game.getOutcome() == null || game.getOutcome().isDraw()) {
            result.addProperty("winner", (String) null);
            result.addProperty("draw", true);
        } else {
            result.addProperty("winner", game.getOutcome().getWinningLobbyPlayer().getName());
            result.addProperty("draw", false);
        }
        for (Bridge b : bridges) {
            b.notify(result.deepCopy());
        }
        System.out.println(result);
    }

    private static void putPair(Map<String, String> into, String arg, String flag) {
        String[] kv = split(arg, flag);
        into.put(kv[0], kv[1]);
    }

    private static String[] split(String arg, String flag) {
        int eq = arg.indexOf('=');
        if (eq <= 0) {
            throw new IllegalArgumentException(flag + " expects SEAT=value, got " + arg);
        }
        return new String[] { arg.substring(0, eq), arg.substring(eq + 1) };
    }

    private GauntletMain() {
    }
}
