let
  nixpkgs = builtins.fetchTarball {
    url    = "https://github.com/NixOS/nixpkgs/archive/228feb709b63a8f0a80b55d9248d8b05dfa70c2e.tar.gz";
    sha256 = "1x97z84z8gncj4ginkzvms1zy1aidmwyk0blxssfbzl5z87x62jh";
  };
  pkgs = import nixpkgs {};
in
  pkgs.mkShell {
    nativeBuildInputs = [
      (pkgs.python3.withPackages (ps: [
        ps.pillow
        ps.pysocks

        ps.websockets
        ps.pyjwt
        ps.grpcio
      ]))

      pkgs.libxml2
    ];
  }
