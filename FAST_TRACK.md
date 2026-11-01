# Ruta rápida: de la planilla escaneada al formulario

Este es el recorrido completo contado como una historia, de principio a fin. No es una referencia de comandos:
para el detalle de cada paso, vea el [README](README.md).

> Los comandos se escriben como `pc ...`. En Windows escriba `pc.cmd ...` y en macOS `./pc ...`.

## 1. Empezar desde cero (solo desarrolladores)

Si usted desarrolla el programa y quiere repetir la instalación como si acabara de descargarlo, este script
revoca el permiso de Google y mueve `.venv`, `.env`, `credentials.json` y `local_data/` a una carpeta de respaldo.
No borra nada en Google Drive. Un usuario normal **no** necesita este paso.

```
scripts/dev-reset.sh
```

Para recuperar lo movido: `scripts/dev-reset.sh --restore <carpeta de respaldo>`.

## 2. Instalar

En Windows haga doble clic en `install.cmd` (usa `install.ps1`). En macOS abra Terminal en la carpeta y ejecute
el instalador. Instala Python, las dependencias y Chromium; no hace falta instalar nada más.

```
./install.sh
```

## 3. Credenciales y configuración

Consiga una clave de OpenRouter y el archivo `credentials.json` de Google (README, secciones 3 y 4) y ponga
`credentials.json` en la carpeta del programa. Luego ejecute el asistente: pide la clave y la URL del formulario,
abre el navegador para iniciar sesión en Google y crea en su Drive la carpeta **Plan de Cuidado** con el tracker.

```
pc setup
```

## 4. Analizar las planillas

Suba los PDF escaneados a la carpeta `Plan de Cuidado/01_inbox` de su Drive. El OCR lee cada página, agrega una
fila por paciente al tracker y mueve el PDF a `02_processed` (o a `03_failed`, con un `.txt` que explica el motivo).

```
pc ocr
```

## 5. Datos fijos (paso manual)

Abra el tracker y complete la pestaña **Datos fijos**: las respuestas iguales para todos los pacientes (médico,
tipo de visita, EPS...). Lo que no escriba allí se toma de `static_defaults` en `config/field_mapping.yaml`; si
hay valor en la pestaña, la pestaña gana. Después complete el tracker; esto también calcula edad, curso de vida
y nombre del territorio. Solo se llenan celdas vacías.

```
pc fill-static
```

> **Antes de enviar al formulario real:** en `static_defaults` hay valores marcados como `TEST ONLY`
> (`fecha_de_nacimiento`, `sexo`, `fecha_de_canalizacion`) que son de prueba: reemplácelos por el dato real de cada
> paciente en el tracker (o borre esas líneas). El médico (`nombre_del_medico_que_atiende_al_paciente`) también está
> marcado `TODO`: escriba el real en **Datos fijos**.

## 6. Validar

El programa revisa cada fila, pinta las celdas con problemas y deja la fila en `READY` (lista) o
`NEEDS_REVIEW` (falta algo).

```
pc validate
```

## 7. Revisar el tracker por colores

Corrija las celdas en color y vuelva a ejecutar `pc validate` hasta que las filas queden en `READY`. Pase el mouse
sobre una celda para ver la nota con el motivo. La misma leyenda está en la pestaña **Leyenda**.

| Color de la celda | Qué significa | Qué hacer |
|---|---|---|
| Rojo | Pregunta obligatoria sin respuesta. | Escriba el dato (o complételo en **Datos fijos**). |
| Naranja | Valor fuera de las opciones del formulario o con formato inválido. | Corrija el valor; use la lista desplegable. |
| Amarillo | El OCR leyó algo que no es válido y dejó la celda vacía. | Mire la nota y escriba el valor correcto. |
| Morado | Los modelos de OCR leyeron distinto un dato crítico, o la ficha de un integrante no coincide con su familia. | Compare con la planilla. Si está mal, corríjalo; si está bien, borre esa clave de la columna `ocr_disagreements`. |

La columna `status` también tiene color: azul = `READY`, amarillo = `NEEDS_REVIEW`, verde = `SUBMITTED`,
rojo = `FAILED`.

## 8. Ensayo seguro (sandbox)

Antes de tocar el formulario real, el programa crea en su carpeta `sandbox/` una copia llamada `[SANDBOX] ...`,
le envía algunas filas `READY`, lee las respuestas y las compara con el tracker. No cambia el estado de las filas.

```
pc sandbox --rows 3
```

## 9. Enviar al formulario real

Cuando el ensayo salga bien, envíe primero **un solo paciente**, revise en el formulario que llegó correcto y
luego envíe el resto. Cada fila enviada queda `SUBMITTED` y nunca se vuelve a enviar.

```
pc submit --limit 1
```

> **Si se salta el sandbox:** use `pc submit --dry-run --limit 1` para ver el enlace pre-llenado sin enviar nada,
> y luego `pc submit --limit 1`. `pc submit` envía al formulario de `FORM_URL` (el que dio en `pc setup`); no use
> `--form-url`, que es solo para enviar a otro formulario (por ejemplo la copia sandbox, y eso sí marca las filas
> como `SUBMITTED`). Si el formulario pide iniciar sesión, agregue `--engine browser`. Cuando el primero esté bien:
> `pc submit`.

Para ver cuántas filas hay en cada estado: `pc status`.

---

Detalle completo, problemas frecuentes y configuración avanzada: [README.md](README.md).
