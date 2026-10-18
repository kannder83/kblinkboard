from typing import Annotated

import typer

from keyboard_light.device import MODES, Keyboard, KeyboardError, LedParam

app = typer.Typer(
    help="Cambia las luces del teclado Epomaker TH80 (conectado por cable USB).",
    no_args_is_help=True,
)

state = {"swapped": False}

Speed = Annotated[int | None, typer.Option("--velocidad", "-v", min=0, max=4, help="0 lento .. 4 rápido")]
Brightness = Annotated[int | None, typer.Option("--brillo", "-b", min=0, max=4, help="0 .. 4")]


def parse_color(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        raise typer.BadParameter("Usa un color hex de 6 dígitos, por ejemplo ff0080.")
    try:
        return tuple(bytes.fromhex(value))
    except ValueError:
        raise typer.BadParameter(f"Color no válido: {value}")


def show(led: LedParam) -> None:
    color = "arcoíris" if led.rainbow else f"#{led.hex_color}"
    typer.echo(
        f"modo={led.mode_name}  color={color}  brillo={led.brightness}/4  "
        f"velocidad={led.speed}/4  opción={led.option}"
    )


def run(action):
    try:
        with Keyboard(swapped_flags=state["swapped"]) as kb:
            return action(kb)
    except KeyboardError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.callback()
def main(
    invertir: Annotated[
        bool,
        typer.Option(
            "--invertir",
            envvar="KEYBOARD_LIGHT_INVERTIR",
            help="Úsalo si 'color' pone varios colores en vez de uno (otros firmwares).",
        ),
    ] = False,
):
    state["swapped"] = invertir


@app.command()
def info():
    """Muestra el teclado y la configuración actual de luces (solo lectura)."""

    def action(kb: Keyboard):
        typer.echo(f"ID de dispositivo: {kb.identify()}")
        typer.echo(f"Firmware: {kb.revision()}")
        typer.echo(f"Retroiluminación: {'encendida' if kb.backlight_on() else 'apagada'}")
        show(kb.get_led())

    run(action)


@app.command()
def modos():
    """Lista los modos de luz disponibles."""
    for name, number in MODES.items():
        typer.echo(f"{number:>3}  {name}")


@app.command()
def color(
    hex_color: Annotated[str, typer.Argument(metavar="COLOR", help="Hex, por ejemplo ff0080")],
    brillo: Brightness = None,
):
    """Pone todo el teclado en un color fijo."""
    r, g, b = parse_color(hex_color)

    def action(kb: Keyboard):
        current = kb.get_led()
        led = LedParam(
            mode=MODES["estatico"],
            speed=current.speed,
            brightness=current.brightness if brillo is None else brillo,
            option=0,
            rainbow=False,
            r=r,
            g=g,
            b=b,
        )
        kb.set_led(led)
        typer.echo("Enviado: ", nl=False)
        show(led)

    run(action)


@app.command()
def modo(
    nombre: Annotated[str, typer.Argument(help="Nombre del modo (ver 'modos')")],
    color_hex: Annotated[str | None, typer.Option("--color", "-c", help="Color hex")] = None,
    arcoiris: Annotated[bool, typer.Option("--arcoiris", "-a", help="Colores cambiantes")] = False,
    velocidad: Speed = None,
    brillo: Brightness = None,
    opcion: Annotated[
        int | None, typer.Option("--opcion", "-o", min=0, max=15, help="Dirección o variante del modo")
    ] = None,
):
    """Cambia el efecto de luz. Lo que no indiques se conserva."""
    if nombre not in MODES:
        raise typer.BadParameter(f"Modo desconocido. Opciones: {', '.join(MODES)}")
    rgb = parse_color(color_hex) if color_hex else None

    def action(kb: Keyboard):
        current = kb.get_led()
        r, g, b = rgb or (current.r, current.g, current.b)
        led = LedParam(
            mode=MODES[nombre],
            speed=current.speed if velocidad is None else velocidad,
            brightness=current.brightness if brillo is None else brillo,
            option=0 if opcion is None else opcion,
            rainbow=arcoiris or (rgb is None and current.rainbow),
            r=r,
            g=g,
            b=b,
        )
        kb.set_led(led)
        typer.echo("Enviado: ", nl=False)
        show(led)

    run(action)


@app.command()
def brillo(nivel: Annotated[int, typer.Argument(min=0, max=4, help="0 .. 4")]):
    """Cambia solo el brillo."""

    def action(kb: Keyboard):
        led = kb.get_led()
        led.brightness = nivel
        kb.set_led(led)
        typer.echo("Enviado: ", nl=False)
        show(led)

    run(action)


@app.command()
def apagar():
    """Apaga la retroiluminación."""
    run(lambda kb: kb.set_backlight(False))
    typer.echo("Luces apagadas.")


@app.command()
def encender():
    """Enciende la retroiluminación."""
    run(lambda kb: kb.set_backlight(True))
    typer.echo("Luces encendidas.")
