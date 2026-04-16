"""TournamentDirector — orchestrates multi-table tournament with table breaking.

Manages player seating, parallel table tasks, blind clock,
table breaking (when ≤2 players), and final table formation.
"""

from __future__ import annotations

import asyncio
import logging
import math

from telegram import Bot

from dealer_bot import (
    AgentInfo, TableSession, GameState,
    get_blinds, _send, MAIN_GROUP_ID, STARTING_STACK, ACTION_TIMEOUT,
)

log = logging.getLogger("dealer.tournament")


class TournamentDirector:
    """Runs a multi-table tournament to completion."""

    def __init__(
        self,
        agents: list[AgentInfo],
        bot: Bot,
        ws_connections: dict,
        table_size: int = 6,
    ):
        self.all_agents = agents
        self.bot = bot
        self.ws_connections = ws_connections
        self.table_size = table_size

        self.tables: dict[int, TableSession] = {}
        self._table_tasks: dict[int, asyncio.Task] = {}
        self._next_table_id = 1
        self._blind_level_round = 0  # global round counter for blind schedule
        self._eliminated: list[AgentInfo] = []  # ordered by elimination

    async def run(self):
        """Main entry point — runs tournament from seating to winner."""
        try:
            self._seat_players()
            await self._announce_seating()

            # Run all tables in parallel
            for tid, table in self.tables.items():
                task = asyncio.create_task(self._run_table(tid))
                self._table_tasks[tid] = task

            # Wait for all table tasks to complete
            while self._table_tasks:
                done, _ = await asyncio.wait(
                    self._table_tasks.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    # Find which table finished
                    finished_tid = None
                    for tid, t in self._table_tasks.items():
                        if t is task:
                            finished_tid = tid
                            break
                    if finished_tid is not None:
                        del self._table_tasks[finished_tid]

                    # Check for table breaking
                    await self._check_table_breaking()

            # Tournament complete
            alive = [a for a in self.all_agents if a.stack > 0]
            if alive:
                winner = max(alive, key=lambda a: a.stack)
                log.info("=== TOURNAMENT OVER. Winner: @%s with %d chips ===",
                         winner.username, winner.stack)
                await _send(self.bot, MAIN_GROUP_ID,
                            f"🏆 Tournament over!\n@{winner.username} wins with {winner.stack} chips!")
            else:
                await _send(self.bot, MAIN_GROUP_ID, "🏆 Tournament over!")

        except asyncio.CancelledError:
            log.info("Tournament cancelled")
            for task in self._table_tasks.values():
                task.cancel()
        except Exception as e:
            log.error("Tournament crashed: %s", e, exc_info=True)
            await _send(self.bot, MAIN_GROUP_ID, f"❌ Tournament error: {e}")

    def _seat_players(self):
        """Distribute players across tables."""
        n = len(self.all_agents)
        num_tables = max(1, math.ceil(n / self.table_size))

        # Deal players round-robin across tables
        table_agents: dict[int, list[AgentInfo]] = {i + 1: [] for i in range(num_tables)}
        for i, agent in enumerate(self.all_agents):
            tid = (i % num_tables) + 1
            table_agents[tid].append(agent)

        for tid, agents in table_agents.items():
            self.tables[tid] = TableSession(
                table_id=tid, agents=agents, bot=self.bot,
                ws_connections=self.ws_connections,
            )
            self._next_table_id = max(self._next_table_id, tid + 1)

        log.info("Seated %d players across %d tables", n, num_tables)

    async def _announce_seating(self):
        lines = [f"📋 Seating ({len(self.tables)} tables):"]
        for tid, table in sorted(self.tables.items()):
            names = ", ".join(f"@{a.username}" for a in table.agents)
            lines.append(f"  Table {tid}: {names}")
        await _send(self.bot, MAIN_GROUP_ID, "\n".join(lines))

        # Notify WS bots of their table assignment
        for tid, table in self.tables.items():
            for agent in table.agents:
                ws = self.ws_connections.get(agent.username.lower())
                if ws:
                    try:
                        import json
                        await ws.send(json.dumps({
                            "type": "tournament_start",
                            "players": len(self.all_agents),
                            "tables": len(self.tables),
                            "your_table": tid,
                        }))
                    except Exception:
                        pass

    async def _run_table(self, tid: int):
        """Run rounds at a single table until ≤1 player or table is broken."""
        table = self.tables[tid]
        current_sb, current_bb = get_blinds(1)
        table.engine.set_blinds(current_sb, current_bb)
        dealer_idx = 0

        while True:
            active = [a for a in table.agents if a.stack > 0]

            if len(active) < 2:
                log.info("[T%d] Table closing — %d player(s) left", tid, len(active))
                break

            # Blind schedule based on global round counter
            self._blind_level_round += 1
            new_sb, new_bb = get_blinds(self._blind_level_round)
            if new_sb != current_sb:
                current_sb, current_bb = new_sb, new_bb
                table.engine.set_blinds(current_sb, current_bb)
                await _send(self.bot, MAIN_GROUP_ID,
                           f"⬆️ Blinds increased to {current_sb}/{current_bb}!")

            n = len(active)
            sb_pos = dealer_idx % n
            rotated = active[sb_pos:] + active[:sb_pos]

            await table.run_single_round(rotated, current_sb, current_bb)
            dealer_idx += 1

            # Sync stacks
            for p in table.engine.players:
                agent = table._by_player_id.get(p.id)
                if agent:
                    agent.stack = p.stack

            # Detect eliminations
            for a in table.agents:
                if a.stack == 0 and a in active:
                    self._eliminated.append(a)
                    place = len(self.all_agents) - len(self._eliminated) + 1
                    await _send(self.bot, MAIN_GROUP_ID,
                                f"💀 @{a.username} eliminated ({_ordinal(place)} place)")
                    ws = self.ws_connections.get(a.username.lower())
                    if ws:
                        try:
                            import json
                            await ws.send(json.dumps({
                                "type": "eliminated",
                                "place": place,
                                "players_left": len(self.all_agents) - len(self._eliminated),
                            }))
                        except Exception:
                            pass

            # Chip counts
            alive_all = sorted(
                [a for a in self.all_agents if a.stack > 0],
                key=lambda x: x.stack, reverse=True,
            )
            if len(alive_all) >= 2:
                lines = [f"Chip counts ({len(alive_all)} players):"]
                for a in alive_all[:10]:  # top 10
                    lines.append(f"  @{a.username}: {a.stack}")
                if len(alive_all) > 10:
                    lines.append(f"  ... and {len(alive_all) - 10} more")
                await _send(self.bot, MAIN_GROUP_ID, "\n".join(lines))

            await asyncio.sleep(3.0)

    async def _check_table_breaking(self):
        """After a table finishes, move remaining players to other tables."""
        tables_to_break = []
        for tid, table in list(self.tables.items()):
            alive = [a for a in table.agents if a.stack > 0]
            # Break table if it has ≤1 alive AND other tables exist
            if len(alive) <= 1 and len(self.tables) > 1:
                tables_to_break.append((tid, alive))

        for tid, survivors in tables_to_break:
            if not survivors:
                del self.tables[tid]
                log.info("[T%d] Table removed (empty)", tid)
                continue

            # Find table with fewest players to seat survivors
            remaining_tables = {
                t: table for t, table in self.tables.items()
                if t != tid and t in self._table_tasks
            }

            if not remaining_tables:
                # No other active tables — this player wins or waits
                # Check if we need a final table
                all_alive = [a for a in self.all_agents if a.stack > 0]
                if len(all_alive) <= self.table_size:
                    await self._create_final_table(all_alive)
                break

            for survivor in survivors:
                # Find smallest table
                target_tid = min(
                    remaining_tables,
                    key=lambda t: len([a for a in self.tables[t].agents if a.stack > 0]),
                )
                target = self.tables[target_tid]
                target.agents.append(survivor)
                target._by_player_id[survivor.player_id] = survivor

                await _send(self.bot, MAIN_GROUP_ID,
                            f"🔀 @{survivor.username} moved to Table {target_tid}")
                log.info("Moved @%s from T%d to T%d", survivor.username, tid, target_tid)

            del self.tables[tid]
            log.info("[T%d] Table broken", tid)

        # Check if we should form final table
        active_tables = [t for t, table in self.tables.items()
                         if len([a for a in table.agents if a.stack > 0]) >= 2]
        all_alive = [a for a in self.all_agents if a.stack > 0]

        if len(all_alive) <= self.table_size and len(active_tables) > 1:
            # Cancel running tables and form final table
            for task in self._table_tasks.values():
                task.cancel()
            self._table_tasks.clear()
            await self._create_final_table(all_alive)

    async def _create_final_table(self, players: list[AgentInfo]):
        """Create and run the final table."""
        await _send(self.bot, MAIN_GROUP_ID,
                    f"🎯 Final table! {len(players)} players remaining")

        # Clear old tables
        self.tables.clear()
        self._table_tasks.clear()

        # Create final table
        final_tid = self._next_table_id
        self.tables[final_tid] = TableSession(
            table_id=final_tid, agents=players, bot=self.bot,
            ws_connections=self.ws_connections,
        )

        task = asyncio.create_task(self._run_table(final_tid))
        self._table_tasks[final_tid] = task

    def stop(self):
        """Cancel all running table tasks."""
        for task in self._table_tasks.values():
            task.cancel()
        for table in self.tables.values():
            table.stop()


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd'][min(n % 10, 4)] if n % 10 < 4 else 'th'}"
