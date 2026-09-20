/*
 * Gauntlet bridge for Forge.
 *
 * This file is part of a program that links against Forge and is therefore
 * licensed GPLv3-or-later, matching Forge itself. The Python side of Gauntlet
 * is a separate process communicating over a socket and is not covered by this.
 */
package forge.gauntlet;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicLong;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

/**
 * The single socket every bridged seat talks over.
 *
 * One line of JSON out, one line of JSON back, strictly synchronised. Forge
 * calls its controllers on the game thread and blocks, so a lock around the
 * round trip is all the concurrency control this needs.
 *
 * Nothing here ever throws into Forge. A dead socket, a timeout or a malformed
 * reply all surface as a null response, and the caller falls back to Forge's
 * own AI. A game must never stall because the other end went away.
 */
public final class Bridge implements AutoCloseable {

    /** Bumped when the wire format changes in a way an old peer cannot read. */
    public static final int PROTOCOL_VERSION = 1;

    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();

    private final Socket socket;
    private final BufferedReader in;
    private final Writer out;
    private final AtomicLong nextId = new AtomicLong(1);
    private final Object lock = new Object();

    private volatile boolean broken = false;

    private Bridge(Socket socket) throws IOException {
        this.socket = socket;
        this.in = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
        this.out = new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8);
    }

    /**
     * Connects to the orchestrator.
     *
     * @param endpoint host:port, loopback only
     * @param timeoutMs how long a single decision may take before we give up on
     *                  it and let Forge's AI decide. Generous by default: an
     *                  agent thinking is the normal case, not an error.
     */
    public static Bridge connect(String endpoint, int timeoutMs) throws IOException {
        int colon = endpoint.lastIndexOf(':');
        if (colon < 0) {
            throw new IOException("bridge endpoint must be host:port, got " + endpoint);
        }
        String host = endpoint.substring(0, colon);
        int port = Integer.parseInt(endpoint.substring(colon + 1));

        Socket s = new Socket();
        s.connect(new InetSocketAddress(host, port), 10_000);
        s.setTcpNoDelay(true);
        s.setSoTimeout(timeoutMs);
        return new Bridge(s);
    }

    public boolean isBroken() {
        return broken;
    }

    /**
     * Sends one decision request and waits for the answer.
     *
     * @return the response object, or null if the seat did not answer in time,
     *         answered with something unreadable, or the socket is gone. Null
     *         means "fall back to Forge", never "crash".
     */
    public JsonObject ask(JsonObject request) {
        if (broken) {
            return null;
        }
        long id = nextId.getAndIncrement();
        request.addProperty("v", PROTOCOL_VERSION);
        request.addProperty("id", id);

        synchronized (lock) {
            try {
                out.write(GSON.toJson(request));
                out.write('\n');
                out.flush();

                String line = in.readLine();
                if (line == null) {
                    broken = true;
                    return null;
                }
                JsonObject reply = JsonParser.parseString(line).getAsJsonObject();

                // A reply for the wrong decision means the two sides have lost
                // sync, which no single fallback can repair. Shut the bridge.
                if (!reply.has("id") || reply.get("id").getAsLong() != id) {
                    broken = true;
                    return null;
                }
                return reply;
            } catch (Exception e) {
                // Timeouts are expected and recoverable; everything else is not.
                if (!(e instanceof java.net.SocketTimeoutException)) {
                    broken = true;
                }
                return null;
            }
        }
    }

    /**
     * Fire-and-forget notification. Used for transcript events that need no
     * answer, such as a card being drawn or the game ending.
     */
    public void notify(JsonObject event) {
        if (broken) {
            return;
        }
        event.addProperty("v", PROTOCOL_VERSION);
        event.addProperty("id", 0);
        synchronized (lock) {
            try {
                out.write(GSON.toJson(event));
                out.write('\n');
                out.flush();
            } catch (IOException e) {
                broken = true;
            }
        }
    }

    @Override
    public void close() {
        try {
            socket.close();
        } catch (IOException ignored) {
            // Closing a socket we are done with cannot fail in a way we care about.
        }
    }
}
