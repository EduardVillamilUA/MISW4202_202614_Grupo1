"""
ejecutar_experimento.py — orquesta las 60 corridas formales del experimento.

Para cada uno de los 3 tipos de falla, ejecuta 20 corridas:
  1. Reinicia el estado del stack a "sano" (clear-fault en las 3 instancias).
  2. Espera a que ms-monitor confirme que las 3 instancias están healthy.
  3. Ejecuta harness.py con el tipo de falla y número de corrida correspondiente.
  4. Espera a que termine antes de iniciar la siguiente.

Al finalizar las 60 corridas, invoca analizar_resultados.py automáticamente.

Uso (dentro del contenedor 'harness', o localmente contra los puertos publicados
del stack):
    python ejecutar_experimento.py \
        --router-url http://ms-router:8000 \
        --monitor-status-url http://ms-monitor:6000/status \
        --instancia-objetivo instancia-3 \
        --instancia-objetivo-url http://ms-cotizacion-3:5000 \
        --instancias instancia-1=http://ms-cotizacion-1:5000,instancia-2=http://ms-cotizacion-2:5000,instancia-3=http://ms-cotizacion-3:5000 \
        --directorio-resultados /resultados
"""
import argparse
import subprocess
import sys
import time

import requests

TIPOS_FALLA = ["timeout", "error_http", "prima_inconsistente"]
REPETICIONES_POR_TIPO = 20
MAX_INTENTOS_ESPERA_SANO = 15  # * 2s = 30s máximo
INTERVALO_ESPERA_SANO_SEGUNDOS = 2


def parse_args():
    p = argparse.ArgumentParser(description="Orquesta las 60 corridas del experimento")
    p.add_argument("--router-url", required=True)
    p.add_argument("--monitor-status-url", required=True)
    p.add_argument("--instancia-objetivo", default="instancia-3")
    p.add_argument("--instancia-objetivo-url", required=True)
    p.add_argument("--instancias", required=True, help="instancia-1=http://...,instancia-2=http://...,...")
    p.add_argument("--directorio-resultados", default="/resultados")
    return p.parse_args()


def parse_instancias(valor: str):
    resultado = {}
    for par in valor.split(","):
        k, v = par.split("=", 1)
        resultado[k.strip()] = v.strip()
    return resultado


def reiniciar_estado_limpio(instancias: dict, monitor_status_url: str):
    """
    Deja el stack en estado limpio y conocido antes de una corrida: limpia
    cualquier falla activa en las tres instancias y espera a que el Monitor
    confirme que las tres están 'healthy'. Devuelve True si lo confirmó.
    """
    for inst_id, url_base in instancias.items():
        try:
            requests.post(f"{url_base}/clear-fault", timeout=3)
        except requests.exceptions.RequestException as e:
            print(f"[ADVERTENCIA] No se pudo limpiar falla en {inst_id}: {e}")

    for intento in range(MAX_INTENTOS_ESPERA_SANO):
        try:
            resp = requests.get(monitor_status_url, timeout=3)
            estados = resp.json()
            if all(v.get("estado") == "healthy" for v in estados.values()):
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(INTERVALO_ESPERA_SANO_SEGUNDOS)

    return False


def main():
    args = parse_args()
    instancias = parse_instancias(args.instancias)

    total_corridas = len(TIPOS_FALLA) * REPETICIONES_POR_TIPO
    corrida_actual = 0
    inicio_total = time.monotonic()

    for tipo_falla in TIPOS_FALLA:
        for numero_corrida in range(1, REPETICIONES_POR_TIPO + 1):
            corrida_actual += 1
            print(f"\n=== Corrida {corrida_actual}/{total_corridas}: {tipo_falla} #{numero_corrida} ===")

            print("Reiniciando estado del stack a 'sano'...")
            sano = reiniciar_estado_limpio(instancias, args.monitor_status_url)
            if not sano:
                print(
                    f"[ERROR] El stack no reportó estado 'healthy' en las 3 instancias tras "
                    f"{MAX_INTENTOS_ESPERA_SANO * INTERVALO_ESPERA_SANO_SEGUNDOS}s. "
                    f"Deteniendo la ejecución para no acumular corridas inválidas."
                )
                sys.exit(1)

            archivo_log = f"{args.directorio_resultados}/harness-corrida-{tipo_falla}-{numero_corrida}.jsonl"

            comando = [
                sys.executable, "harness.py",
                "--tipo-falla", tipo_falla,
                "--numero-corrida", str(numero_corrida),
                "--router-url", args.router_url,
                "--instancia-objetivo", args.instancia_objetivo,
                "--instancia-objetivo-url", args.instancia_objetivo_url,
                "--archivo-log", archivo_log,
            ]
            resultado = subprocess.run(comando)
            if resultado.returncode != 0:
                print(f"[ERROR] harness.py terminó con código {resultado.returncode} en la corrida {tipo_falla} #{numero_corrida}.")
                sys.exit(1)

    duracion_total_min = (time.monotonic() - inicio_total) / 60
    print(f"\n=== Las {total_corridas} corridas terminaron en {duracion_total_min:.1f} minutos ===")

    print("\nEjecutando analizar_resultados.py...")
    resultado = subprocess.run([sys.executable, "analizar_resultados.py", "--directorio-resultados", args.directorio_resultados])
    sys.exit(resultado.returncode)


if __name__ == "__main__":
    main()
