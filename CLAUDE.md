# Errand Agent

- **Status:** Read [BUILD_LOG.md](BUILD_LOG.md) first. It lists what is built and what is left against every requirement.
- **Keep it current:** Update `BUILD_LOG.md` in the same change as the work. Move a status only when there is proof, and add a dated log entry.
- **Bugs:** Add real bugs to [BUGS.md](BUGS.md) as they happen, with the regression test. They are a deliverable.
- **Spec:** [docs/blueprint.html](docs/blueprint.html) is the design. Follow it closely, and note any change under "Changes from the blueprint" in the build log.
- **Server code:** Read [server/app/CLAUDE.md](server/app/CLAUDE.md) before changing it.
- **Mock provider:** `server/mock_provider/` stands in for an outside company. `app` reaches it only over HTTP. Its tool contract comes from the brief and must not change.
- **Web code:** `web/src/domains/` mirrors the server domains. UI tests fake only the HTTP boundary.
- **Checks:** `make check`, then `make test-integration`. For the running stack, `make smoke`.
