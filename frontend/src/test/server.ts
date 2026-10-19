import { setupServer } from 'msw/node';
import { createDb } from '@/mocks/db';
import { createHandlers, type Scenario } from '@/mocks/handlers';

let scenario: Scenario = 'normal';

/** Mock backend for tests: the same handlers as dev, fresh data per test, no pauses in streams. */
export const server = setupServer();

export function resetServer(): void {
  scenario = 'normal';
  server.resetHandlers(...createHandlers({ db: createDb(), scenario: () => scenario, tick: 0 }));
}

/** Switches the mock backend to another scenario for the current test. */
export function useScenario(next: Scenario): void {
  scenario = next;
}
