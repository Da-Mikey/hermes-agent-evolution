# nix/packages.nix — Hermes Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    {
      pkgs,
      lib,
      inputs',
      ...
    }:
    let

      sandbox = pkgs.callPackage ./sandbox.nix { };

      minimal = pkgs.callPackage ./hermes-agent.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
        npm-lockfile-fix = inputs'.npm-lockfile-fix.packages.default;
        # Only embed clean revs — dirtyRev doesn't represent any upstream
        # commit, so comparing it would always claim "update available".
        rev = inputs.self.rev or null;
      };

      # All platform-portable optional integrations pre-built.
      full = minimal.override {
        extraDependencyGroups = [
          "anthropic"
          "azure-identity"
          "bedrock"
          "daytona"
          "dingtalk"
          "edge-tts"
          "exa"
          "fal"
          "feishu"
          "firecrawl"
          "hindsight"
          "honcho"
          "messaging"
          "modal"
          "parallel-web"
          "tts-premium"
          "vercel"
          "voice"
        ]
        # matrix is Linux-only (oqs/liboqs lacks aarch64-darwin wheels).
        ++ lib.optionals pkgs.stdenv.isLinux [ "matrix" ];
      };
    in
    {
      packages = {
        node-gyp =
          (pkgs.callPackage ./lib.nix {
            inherit (pkgs) npm-lockfile-fix;
          }).node-gyp;
        default = full;

        inherit minimal;

        # Ships discord.py + python-telegram-bot + slack-sdk so a plain
        # `nix profile install .#messaging` connects to Discord/Telegram/Slack
        # on first run — lazy-install can't write to the read-only /nix/store.
        messaging = minimal.override {
          extraDependencyGroups = [ "messaging" ];
        };

        tui = full.hermesTui;
        web = full.hermesWeb;
        desktop = full.hermesDesktop;

        update-npm-lockfile = full.hermesNpmLib.updateNpmLockfile;
      }
      # The dev sandbox is Linux-only: it pulls bubblewrap/slirp4netns (and
      # the X11 electron runtime) unconditionally, and nixpkgs refuses to
      # evaluate those on darwin. Gate the package to Linux so
      # `nix flake check` on macOS stops failing with "Refusing to evaluate
      # package 'bubblewrap-0.11.2' ... not available on the requested
      # hostPlatform" (#79). The `let`-bound `sandbox` stays lazy, so it is
      # never forced on darwin.
      // lib.optionalAttrs pkgs.stdenv.isLinux { inherit sandbox; };
    };
}
