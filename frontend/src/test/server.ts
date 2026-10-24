import { FetchInterceptor } from '@mswjs/interceptors/fetch/web';
import { defineNetwork, InterceptorSource } from 'msw/experimental';
import { AsyncHandlersController, defaultNetworkOptions } from 'msw/node';
import { createDb } from '@/mocks/db';
import { createHandlers, type Scenario } from '@/mocks/handlers';

let scenario: Scenario = 'normal';

// The browser build of the interceptors declares its own Interceptor class; at runtime it is the same API.
type SourceInterceptor = ConstructorParameters<typeof InterceptorSource>[0]['interceptors'][number];

/**
 * Mock backend for tests: the same handlers as dev, fresh data per test, no pauses in streams.
 *
 * `setupServer()` intercepts Node fetch at the socket level, and its protocol detection lets a
 * fresh socket through to the real network when Undici writes the request a tick late (e.g. the
 * first request after an aborted one, whose socket Undici destroys). Tests then hit localhost and
 * see "Нет связи с сервером" at random. The fetch call itself is patched instead, so mocked
 * requests never open a socket.
 */
const network = defineNetwork({
  ...defaultNetworkOptions,
  sources: [new InterceptorSource({ interceptors: [new FetchInterceptor() as unknown as SourceInterceptor] })],
  handlers: new AsyncHandlersController([]),
});

export const server = {
  events: network.events,
  listen({ onUnhandledFrame }: { onUnhandledFrame: 'error' | 'warn' | 'bypass' }): void {
    network.configure({ onUnhandledFrame });
    network.enable();
  },
  use: network.use.bind(network),
  resetHandlers: network.resetHandlers.bind(network),
  close(): void {
    network.disable();
  },
};

export function resetServer(): void {
  scenario = 'normal';
  server.resetHandlers(...createHandlers({ db: createDb(), scenario: () => scenario, tick: 0 }));
}

/** Switches the mock backend to another scenario for the current test. */
export function useScenario(next: Scenario): void {
  scenario = next;
}
