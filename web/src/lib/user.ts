/** One demo user by default. `?user=seed-user` opens the seeded memories. */
export const USER_ID = new URLSearchParams(window.location.search).get("user") ?? "demo-user";
