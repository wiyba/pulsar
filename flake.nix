{
  description = "pulsar";

  inputs.nixpkgs.url = "nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      forAll = f: nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (system: f nixpkgs.legacyPackages.${system});
      python = pkgs: pkgs.python3.withPackages (ps: [ ps.httpx ]);
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.writeShellApplication {
          name = "pulsar";
          runtimeInputs = [ (python pkgs) ];
          text = ''exec python3 ${./pulsar.py} "$@"'';
        };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell { packages = [ (python pkgs) ]; };
      });
    };
}
