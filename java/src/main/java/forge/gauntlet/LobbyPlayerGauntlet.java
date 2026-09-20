/*
 * Gauntlet bridge for Forge. GPLv3-or-later, see Bridge.java.
 */
package forge.gauntlet;

import java.util.Set;

import forge.ai.AIOption;
import forge.ai.LobbyPlayerAi;
import forge.game.Game;
import forge.game.player.Player;
import forge.game.player.PlayerController;

/**
 * Seats a player whose judgment calls are answered over the bridge.
 *
 * Forge builds its in-game {@link Player} objects through the lobby player, so
 * this is the one place a custom controller can be installed without patching
 * Forge itself. Extending {@link LobbyPlayerAi} rather than {@code LobbyPlayer}
 * inherits AI profile handling and the mind-slave path for free.
 */
public class LobbyPlayerGauntlet extends LobbyPlayerAi {

    private final Bridge bridge;
    private final String seat;
    private final Set<String> routedKinds;

    public LobbyPlayerGauntlet(String name, String seat, Bridge bridge, Set<String> routedKinds,
            Set<AIOption> aiOptions) {
        super(name, aiOptions);
        this.seat = seat;
        this.bridge = bridge;
        this.routedKinds = routedKinds;
    }

    @Override
    public Player createIngamePlayer(Game game, final int id) {
        Player player = new Player(getName(), game, id);
        player.setFirstController(new PlayerControllerGauntlet(game, player, this, bridge, seat, routedKinds));
        return player;
    }

    /**
     * A player under someone else's control is still played by that other
     * player's brain, so the mind slave keeps Forge's AI rather than routing to
     * this seat. Handing a seat control of an opponent's turn would need a
     * second bridge conversation and is not worth the complexity yet.
     */
    @Override
    public PlayerController createMindSlaveController(Player master, Player slave) {
        return super.createMindSlaveController(master, slave);
    }
}
