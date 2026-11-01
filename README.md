# Plan de Cuidado: de la planilla escaneada al formulario

Esta herramienta pasa los pacientes de las planillas escaneadas (CamScanner) al formulario de Google
"FORMULARIO PLAN DE CUIDADO INTEGRAL… EBS RESOLUCIÓN 1010-2026 SOACHA A".

> ¿Primera vez? Lea la [ruta rápida](FAST_TRACK.md): el recorrido completo en una página. Este documento es la
> referencia detallada de cada paso.

Funciona así:

1. Usted sube los PDF escaneados a una carpeta de su Google Drive.
2. El programa lee las planillas con inteligencia artificial (OCR) y llena una hoja de cálculo de Google (el **tracker**).
3. Usted completa los datos fijos, revisa las celdas marcadas en color y corrige lo que falte.
4. El programa envía cada paciente al formulario y marca la fila como enviada.

Su Google Drive es la fuente de verdad: los PDF, el tracker y el esquema del formulario viven allí. Si cambia de
computador, instala el programa de nuevo, ejecuta `pc setup` y todo sigue donde lo dejó.

Resumen del uso diario (cada paso se explica abajo):

```
pc ocr                # lee los PDF de 01_inbox y agrega filas al tracker          (sección 6)
pc fill-static        # completa con los datos fijos y calcula edad, curso de vida  (sección 7)
pc validate           # marca en colores y deja cada fila en READY o NEEDS_REVIEW   (sección 8)
pc sandbox --rows 3   # ensayo en una copia del formulario                          (sección 9)
pc submit --limit 1   # envía un paciente; revise y luego `pc submit` para el resto (sección 10)
pc status             # resumen por estado                                          (sección 11)
```

> En este documento los comandos se escriben como `pc ...`. En Windows escriba `pc.cmd ...` y en macOS `./pc ...`.

---

## 1. Lo que necesita antes de empezar

- Un computador con Windows 10/11 o macOS, con acceso a internet (Google y OpenRouter).
- Una cuenta de Google (la misma con la que va a enviar los formularios).
- Una clave de OpenRouter (sección 3). El costo aproximado es de US$ 0,013 por página con los modelos recomendados.
- El archivo `credentials.json` de Google (sección 4). Se crea una sola vez.

No hace falta instalar Python ni ningún otro programa: el instalador se encarga de todo.

## 2. Instalación

### Windows

1. Descargue esta carpeta (botón **Code → Download ZIP** en GitHub) y descomprímala, por ejemplo en `Documentos\planilla-plan-cuidado`.
2. Haga doble clic en **`install.cmd`**. Se abre una ventana negra que instala todo (uv, Python 3.12, dependencias y
   Chromium para el motor `browser`); tarda unos minutos.
3. Cuando diga "Instalación completa", cierre la ventana.

Para usar el programa, abra la carpeta, haga clic en la barra de direcciones del Explorador, escriba `cmd` y presione
Enter. En la ventana que se abre escriba los comandos con `pc.cmd`, por ejemplo `pc.cmd setup`.

### macOS

1. Descargue y descomprima la carpeta, por ejemplo en `Documentos/planilla-plan-cuidado`.
2. Abra la aplicación **Terminal**, escriba `cd ` (con un espacio), arrastre la carpeta a la ventana y presione Enter.
3. Ejecute `./install.sh`.

Después use los comandos con `./pc`, por ejemplo `./pc setup`.

Si no necesita el motor `browser` (sección 10), puede omitir Chromium: `./install.sh --skip-browser` o
`.\install.ps1 -SkipBrowser`.

## 3. Clave de OpenRouter

OpenRouter da acceso a los modelos de inteligencia artificial que leen las planillas.

1. Entre a <https://openrouter.ai> y cree una cuenta.
2. En **Settings → Credits**, cargue saldo (con US$ 5 alcanza para cientos de páginas).
3. En **Settings → Keys**, haga clic en **Create Key**, póngale un nombre (por ejemplo "plan-cuidado") y copie la
   clave (empieza por `sk-or-`).
4. Recomendado: en **Settings → Privacy**, desactive las opciones que permiten a los proveedores guardar o entrenar
   con sus datos (vea también la sección 14).

`pc setup` le pedirá esta clave, la probará y la guardará en el archivo `.env` de la carpeta del programa.

## 4. Credenciales de Google (`credentials.json`)

Se hace una sola vez. Permite que el programa use **su** Drive, **sus** hojas de cálculo y cree formularios de
prueba en **su** cuenta.

1. Entre a <https://console.cloud.google.com> con su cuenta de Google.
2. Arriba, en el selector de proyectos, haga clic en **Proyecto nuevo**, póngale un nombre (por ejemplo "plan-cuidado") y créelo.
3. Active las APIs. En el buscador de arriba escriba cada nombre, ábralo y haga clic en **Habilitar**:
   - **Google Drive API**
   - **Google Sheets API**
   - **Google Forms API**
4. Configure la pantalla de consentimiento: menú **APIs y servicios → Pantalla de consentimiento de OAuth**
   (o **Google Auth Platform → Branding**):
   - Tipo de usuario: **Externo**. Nombre de la app: "plan-cuidado". Correo de asistencia: el suyo.
   - En **Público / Usuarios de prueba**, agregue su propio correo (y el de cada persona que vaya a usar esta credencial).
5. Cree la credencial: **APIs y servicios → Credenciales → Crear credenciales → ID de cliente de OAuth**.
   - Tipo de aplicación: **App de escritorio**. Nombre: "plan-cuidado".
   - Haga clic en **Descargar JSON**.
6. Cambie el nombre del archivo descargado a **`credentials.json`** y cópielo dentro de la carpeta del programa.

Al ejecutar `pc setup` se abrirá el navegador: elija su cuenta. Como la app está "en prueba", Google muestra la
advertencia "Google no verificó esta app": haga clic en **Continuar** y acepte los permisos.

## 5. Configuración inicial: `pc setup`

```
pc setup
```

El asistente:

- pide solo la clave de OpenRouter y la URL del formulario (con Enter conserva el valor actual o acepta el sugerido);
- escribe en `.env` los modelos recomendados, sin preguntarlos: `OCR_MODEL=google/gemini-3.1-flash-lite` y
  `OCR_CONSENSUS_MODEL=qwen/qwen3-vl-235b-a22b-instruct,anthropic/claude-haiku-5.5` (sección 12);
- abre el navegador para iniciar sesión en Google;
- busca en su Drive la carpeta **Plan de Cuidado**; si no existe, la crea;
- descarga las preguntas del formulario y prepara el tracker;
- si el formulario pide iniciar sesión (o usa `--browser-login`), activa el motor `browser` (sección 10).

```
Plan de Cuidado/
├── 01_inbox/               ← aquí sube los PDF escaneados
├── 02_processed/AAAA-MM/   ← PDF ya leídos
├── 03_failed/              ← PDF que no se pudieron leer (+ un .txt con el motivo)
├── config/
│   └── form_schema.json    ← preguntas del formulario (automático)
├── Tracker Plan de Cuidado ← hoja de cálculo de Google (pestañas Tracker, Datos fijos, Leyenda)
└── sandbox/                ← copias de prueba del formulario (sección 9)
```

Se puede repetir sin miedo: reutiliza lo que ya existe (clave, URL, carpetas, tracker) y no duplica nada; los
modelos vuelven siempre a los recomendados. El programa nunca borra archivos de Drive, solo los mueve.

Si el formulario cambia (preguntas nuevas, eliminadas u opciones distintas), ejecute `pc schema`: actualiza
`form_schema.json`, las columnas del tracker y la pestaña **Datos fijos**, y le muestra qué cambió.

## 6. Leer las planillas: `pc ocr`

Suba los PDF a `01_inbox` y ejecute:

```
pc ocr
```

Cada PDF leído pasa a `02_processed/AAAA-MM/` (o a `03_failed/` con un `.txt` que explica el motivo). Un PDF que ya
está en el tracker no se vuelve a leer. Al final verá cuántas filas se agregaron, cuántas se recuperaron con una
segunda lectura, cuántas quedaron como "fila no leída", cuántos datos críticos quedaron en morado y el costo.

Opciones: `--no-consensus` usa solo el modelo principal; `--model <modelo>` cambia el modelo principal para esa ejecución.

### Cómo lee el OCR cada página

1. Lee el encabezado (colaborador, EBS…) con una imagen de la página completa. En esa misma lectura el modelo
   **ubica la tabla**: la fila de títulos de columna, los bordes izquierdo y derecho y la última fila.
2. Corta la tabla en **3 franjas horizontales** que se solapan, recortadas al ancho de la tabla y con la **fila de
   títulos pegada arriba** de cada franja, para que el modelo no corra los valores de columna. Si la tabla no se pudo
   ubicar, usa la zona fija `table_top`/`table_bottom` de `config/field_mapping.yaml`.
3. Une las filas usando el número impreso en la columna **No.** (1 a 25). Las filas impresas vacías se reconocen como vacías.
4. Si falta algún número, vuelve a pedir **solo esa fila**. Si aun así no se lee, agrega al tracker una fila
   `NEEDS_REVIEW` con el error "fila no leída" y la página/fila de origen, para que usted la escriba a mano.
5. **Consenso**: los modelos de `OCR_CONSENSUS_MODEL` leen **solo las columnas críticas** (documento, ficha,
   teléfono, fecha de visita, tipo de documento) sobre las mismas franjas. Si alguno no coincide con el principal, la
   celda queda en **morado** con el valor que leyó la mayoría.
6. **Fichas por familia**: los integrantes de una familia comparten la ficha. Si la ficha de un integrante 2, 3... no
   aparece en ninguna fila anterior de la página, esa celda (y la ficha anterior que difiere en un dígito) también
   queda en **morado**.

`pc ocr` lee **varias páginas a la vez** (4 por defecto: `page_workers` en `config/field_mapping.yaml` u
`OCR_PAGE_WORKERS` en `.env`), con un máximo de pedidos simultáneos a OpenRouter (`max_concurrent_requests`). El
tracker y Drive se escriben un archivo a la vez, en orden (archivo, página, fila), y un PDF se mueve solo cuando
todas sus páginas terminaron.

Si se cae internet durante `pc ocr`, el PDF **se queda en 01_inbox** (no va a `03_failed`) y el comando se detiene
tras dos archivos sin conexión. Vuelva a ejecutar `pc ocr` cuando tenga internet.

## 7. Datos que se repiten: "Datos fijos" y `pc fill-static`

Muchas preguntas del formulario no están en la planilla y son iguales para todos sus pacientes (nombre del médico,
tipo de visita, aceptación del contratista, EPS...). Hay dos lugares para escribirlas:

1. **Pestaña "Datos fijos" del tracker** (recomendado). Tiene una fila por pregunta del formulario:

   | clave | pregunta | valor | obligatoria |
   |---|---|---|---|
   | `tipo_de_visita` | TIPO DE VISITA | *(usted escribe aquí)* | SI |

   Escriba en la columna **valor**; las preguntas de opción tienen una lista desplegable con las opciones exactas.
   Deje en blanco lo que no aplique.
2. **`static_defaults` en `config/field_mapping.yaml`**: valores de respaldo para lo que la pestaña deja en blanco.
   **Si una pregunta tiene valor en la pestaña, la pestaña gana.**

Luego ejecute:

```
pc fill-static
```

Llena solo las celdas vacías (nunca reemplaza lo leído de la planilla ni lo que usted escribió) y calcula la edad,
el curso de vida y el nombre del territorio.

> **Importante antes de enviar al formulario real.** En `static_defaults` hay valores **solo de prueba**, marcados
> `TEST ONLY`: `fecha_de_nacimiento`, `sexo` y `fecha_de_canalizacion`. Son datos de cada paciente: escriba el valor
> real en el tracker (o borre esas líneas del YAML). El médico (`nombre_del_medico_que_atiende_al_paciente`) está
> marcado `TODO`: escriba el nombre real en **Datos fijos**.

`pc setup` y `pc schema` crean o actualizan la pestaña **sin borrar** lo que usted escribió: agregan las preguntas
nuevas y marcan como `[ELIMINADA DEL FORMULARIO]` las que ya no existen (esas se ignoran). Si antes usaba
`config/static_data.json`, sus valores se copian una sola vez a la pestaña y el JSON deja de usarse.

## 8. Validar y revisar el tracker: `pc validate`

```
pc validate
```

Revisa cada fila, pinta las celdas con problemas (con una nota que explica el motivo; pase el mouse sobre la celda)
y deja cada fila en `READY` o `NEEDS_REVIEW`. Corrija en el navegador y vuelva a ejecutar `pc validate` hasta que
las filas queden en `READY`.

### Colores de las celdas

| Color | Significado | Qué hacer |
|---|---|---|
| Rojo | Pregunta obligatoria sin respuesta (normalmente datos que no están en la planilla: fecha de nacimiento, sexo, EPS...). | Escriba el dato (o complételo en **Datos fijos** y repita `pc fill-static`). |
| Naranja | Valor fuera de las opciones del formulario, formato inválido (teléfono, documento, ficha, fecha) o distinto de lo que exigen las respuestas anteriores. | Corrija el valor; use la lista desplegable. |
| Amarillo | Lo que leyó el OCR no es válido (fecha imposible, teléfono que no empieza por 3...) y la celda quedó vacía. | Mire en la nota lo que leyó y escriba el valor correcto. |
| Morado | Los modelos de OCR leyeron distinto un dato crítico, o la ficha de un integrante no coincide con la de su familia. Tiene prioridad sobre los demás colores. | Compare con la planilla. Si el valor está mal, corríjalo (al cambiarlo deja de ser morado); si está bien, borre esa clave de la columna `ocr_disagreements` de la fila. |

La misma leyenda está en la pestaña **Leyenda** del tracker. Cada fila tiene un enlace al PDF original
(`source_file_link`), la página y la fila de la planilla, para abrirla con un clic. Las filas con error "fila no
leída" se escriben a mano mirando la planilla.

### Estados de cada fila (columna `status`)

| Estado | Color | Significado |
|---|---|---|
| `EXTRACTED` | sin color | Recién leída, aún sin validar. |
| `NEEDS_REVIEW` | amarillo | Falta algo o hay celdas en color. |
| `READY` | azul | Lista para enviar. |
| `SUBMITTED` | verde | Enviada. Nunca se vuelve a enviar. |
| `FAILED` | rojo | Falló el envío (el motivo queda en la fila). Para reintentar: `pc validate` (la devuelve a `READY`) y `pc submit`. |

### Formato de los valores

- Fechas: `AAAA-MM-DD` (por ejemplo `2026-08-06`).
- Preguntas de opción: el texto exacto de la opción (use la lista desplegable).
- Preguntas de varias opciones (casillas): separe los valores con `;`.

## 9. Ensayo sin tocar el formulario real: `pc sandbox`

```
pc sandbox --rows 3
```

Crea en su carpeta `sandbox/` de Drive una copia del formulario llamada `[SANDBOX] <título>`, le envía hasta 3 filas
`READY`, lee las respuestas y le muestra una tabla con las diferencias frente al tracker. **No cambia el estado de
las filas.** Cada ejecución crea una copia nueva; puede borrar las viejas de `sandbox/`.

Otra revisión sin enviar nada: `pc submit --dry-run --limit 1` genera el enlace pre-llenado de una fila para
abrirlo y revisarlo a ojo.

## 10. Enviar al formulario real: `pc submit`

Empiece siempre con un solo paciente y revise en el formulario que llegó bien:

```
pc submit --limit 1
pc submit
```

- Envía las filas `READY` al formulario de `FORM_URL` (el que dio en `pc setup`) y marca cada una `SUBMITTED`.
  Antes de enviar vuelve a validar cada fila; las que ya no pasan quedan en `NEEDS_REVIEW` ("devueltas a revisión").
- Si el envío se interrumpe, ejecute `pc submit` otra vez: continúa con las que faltan.
- `--delay <segundos>` cambia la pausa entre envíos (1,5 por defecto; `SUBMIT_DELAY_SECONDS` en `.env`).
- `--form-url <URL>` envía a **otro** formulario (por ejemplo la copia sandbox). Ojo: eso sí marca las filas como
  `SUBMITTED`, así que no lo use para ensayos con filas que luego deba enviar al formulario real.

### Motor de envío

- **http** (por defecto): envía directamente al formulario. Es rápido, pero la respuesta no queda asociada a su cuenta de Google.
- **browser**: usa un navegador Chromium con su sesión de Google. Úselo si el formulario pide iniciar sesión o si
  necesita que la respuesta quede asociada a su cuenta. `pc setup` lo activa solo (`SUBMIT_ENGINE=browser` en
  `.env`) cuando detecta que el formulario pide sesión.

```
pc setup --browser-login       # una sola vez, para iniciar sesión en el navegador del programa
pc submit --engine browser
```

## 11. Seguimiento y volver a leer un PDF: `pc status` y `pc reset`

`pc status` muestra cuántas filas hay en cada estado y el enlace del formulario.

`pc reset` devuelve PDF ya procesados a `01_inbox` para leerlos de nuevo:

```
pc reset --file planilla.pdf               # por nombre (o por id de Drive)
pc reset --file planilla.pdf --clear-cache # además borra la caché de OCR: se vuelve a pagar la lectura
pc reset --all-unsubmitted                 # todos los PDF procesados o fallidos que no tienen filas enviadas
pc reset --file planilla.pdf --yes         # sin pedir confirmación
```

Mueve el PDF de `02_processed` o `03_failed` a `01_inbox` y borra sus filas del tracker. **Nunca borra filas
`SUBMITTED`**: si el PDF tiene filas ya enviadas, se conservan, se le avisa, y el siguiente `pc ocr` agrega solo las
demás. Sin `--clear-cache`, el siguiente `pc ocr` usa la lectura guardada (gratis, mismo resultado). La caché guarda
la petición exacta: si cambia el modelo, la resolución, las franjas o la versión del programa, se vuelve a leer (y pagar).

## 12. Modelos de OCR y `pc ocr-bench` (usuarios avanzados)

`pc setup` deja fija la combinación más exacta medida. Para cambiarla, edite `.env`:

```
OCR_MODEL=google/gemini-3.1-flash-lite                                         # modelo principal
OCR_CONSENSUS_MODEL=qwen/qwen3-vl-235b-a22b-instruct,anthropic/claude-haiku-5.5 # comparan los datos críticos; 'no' = un solo modelo
OCR_FALLBACK_MODEL=                                                            # opcional: solo para páginas con muchos errores
```

Medido con `pc ocr-bench` sobre una página transcrita: 121 de 125 datos críticos correctos, 1 o 2 errores sin marcar
y unos 13 datos críticos en morado por página; unos US$ 0,013 y ~3 minutos por página (casi todo esperando a Qwen;
como se leen varias páginas a la vez, un PDF largo tarda mucho menos por página). Más barato y rápido (~US$ 0,009
por página): `OCR_CONSENSUS_MODEL=qwen/qwen3-vl-235b-a22b-instruct` (unos 2 errores sin marcar por página).

Medidos y descartados como lectores de datos críticos: `google/gemini-3.5-flash-lite`, `google/gemini-3.6-flash` y
`google/gemini-3.1-pro-preview` se equivocan en los mismos dígitos que Gemini 3.1 Flash Lite (y Pro cuesta
~US$ 0,19 por página); `qwen/qwen3-vl-30b-a3b-instruct`, `openai/gpt-6-luna` y `z-ai/glm-5.3-flash` leen mal las
columnas críticas; `deepseek/deepseek-v4.1-flash` entra en bucle; `qwen/qwen3.8-27b` es más caro sin ser mejor;
`google/gemini-2.5-flash-lite` no tiene proveedor con retención cero de datos. Los de Qwen deben ser la versión
**Instruct**, no Thinking.

### Comparar modelos con datos

1. Transcriba a mano una página real (formato en `config/ocr_truth.example.json`) y guárdela en
   `local_data/bench/truth-<nombre>.json`. Antes de confiar en el resultado, verifique las celdas listadas en `"uncertain"`.
2. Ejecute con un PDF local (no hace falta subirlo a Drive):

```
pc ocr-bench planilla-pagina-1.pdf --name pagina-1 --model <modelo-A> --model <modelo-B>
pc ocr-bench planilla-pagina-1.pdf --name pagina-1 --consensus <modelo-A>,<modelo-B>[,<modelo-C>]
pc ocr-bench planilla-pagina-1.pdf --truth /ruta/a/otra-transcripcion.json --model <modelo-A>
```

Usa **el mismo camino que `pc ocr`** (ubicación de la tabla, franjas, segunda lectura, normalización y consenso) y
guarda las respuestas en la caché: repetirlo no vuelve a pagar (`--no-cache` fuerza una lectura nueva). Muestra:

- **Transcripción por campo**: lo que leyó cada modelo comparado con la transcripción, las filas que no leyó y el costo.
- **En el tracker**: el valor que de verdad llegaría al tracker y si la celda quedaría **marcada** (como con
  `pc validate`). La fila "críticos mal SIN marcar" es la importante: datos equivocados que nadie vería. Necesita
  conexión para leer el formulario de Drive.

Con `--consensus A,B[,C]` muestra además la columna de la combinación (A principal; B y C leen solo los datos
críticos, por eso sus columnas propias muestran 0 % en los demás campos). Con dos modelos muestra también el
**desacuerdo** y el **error residual** (los dos coinciden pero ambos se equivocan).

Para probar otro modelo con un PDF de Drive: cambie `OCR_MODEL` en `.env`, haga `pc reset --file <pdf>` y `pc ocr`
(la caché es por modelo).

## 13. Configuración avanzada: `config/field_mapping.yaml`

- `ocr_columns` y `header`: qué columna de la planilla va a qué pregunta del formulario. Por defecto "FECHA DE
  VISITA" = "fecha plan de cuidado" y "DOCUMENTO" = número de documento del paciente. El médico se toma de los datos
  fijos (con `enabled: true` en `header.colaborador` se toma del encabezado de la planilla).
- `choice_aliases`: abreviaturas de la planilla (PT, PA, NV...) y la opción del formulario a la que corresponden.
- `rules`, `ficha_pattern`: reglas de formato (teléfono, documento, patrón del código de ficha, fechas).
- `conditionals`: condiciones entre preguntas (por ejemplo "NO APLICA" cuando no hay discapacidad).
- `derived`: campos calculados por `pc fill-static` (edad, curso de vida, territorio).
- `dedupe_keys`: una fila es duplicada si coinciden documento y fecha de visita.
- `ocr`: páginas a la vez (`page_workers`), pedidos simultáneos (`max_concurrent_requests`), ubicar la tabla
  (`locate_table`), franjas (`strips`, `strip_overlap`), zona de respaldo (`table_top`/`table_bottom`), filas por
  planilla (`expected_rows`), consenso (`consensus_models`, `consensus_critical_only`), fichas por familia
  (`household_check`), ruta de OpenRouter por modelo (`providers`, por ejemplo el punto "flex" de Gemini a mitad de
  precio) y datos críticos (`critical_fields`).
- `static_defaults`: respaldo de los datos fijos (sección 7).

## 14. Privacidad

- Son datos de salud. **Nunca** suba a un repositorio PDF, el archivo `.env`, `credentials.json` ni `token.json`
  (el `.gitignore` ya los excluye, junto con `local_data/` y `template/`).
- En este computador solo se guardan la sesión de Google, la configuración, los registros y la caché del OCR, todo
  dentro de la carpeta `local_data/` del proyecto. Para cambiar la ubicación, defina la variable `PLAN_CUIDADO_HOME`.
- El OCR se pide solo a puntos de OpenRouter con **retención cero de datos** (ZDR: `zdr: true` y
  `data_collection: deny` en cada pedido). Si un modelo no tiene un proveedor ZDR, el pedido falla en vez de enviar
  la imagen a un proveedor que la guarde.

## 15. Problemas frecuentes

| Mensaje | Solución |
|---|---|
| "Ejecute `pc setup`" | Este computador aún no está configurado o la sesión de Google expiró. |
| "Falta credentials.json" | Siga la sección 4 y ponga el archivo en la carpeta del programa. |
| "OpenRouter rechazó la clave" | Revísela en <https://openrouter.ai/settings/keys> y repita `pc setup`. |
| "Google no verificó esta app" | Es normal para apps personales: **Continuar**. Verifique que su correo esté en "Usuarios de prueba". |
| "No hay filas READY para probar" (`pc sandbox`) | Ejecute `pc fill-static` y `pc validate`, y corrija las celdas en color. |
| Un PDF terminó en `03_failed` | Abra el `.txt` de al lado para ver el motivo. Para reintentar: `pc reset --file <nombre>`. |
| Fila con error "fila no leída" | El OCR no pudo leer esa fila: escríbala a mano mirando la planilla (página y fila en la misma fila). |
| Las tildes se ven mal en Windows | Use siempre `pc.cmd`, que configura la consola en UTF-8. |
| "No se pudo conectar con Google/OpenRouter" | Ver "Problemas de conexión" abajo. |
| "Ocurrió un error inesperado" | Vuelva a ejecutar el comando. Si se repite, envíe a soporte el archivo de registro (abajo). |

### Problemas de conexión

El programa reintenta solo cuando la conexión falla por un momento. Si aun así no logra conectarse, revise su
internet (o cambie de red, por ejemplo a los datos del celular) y vuelva a ejecutar **el mismo comando**. Lo ya hecho
no se pierde: `pc setup` reutiliza la carpeta de Drive que alcanzó a crear y nunca se envía dos veces un paciente.
Ejecute el programa en una terminal normal (Terminal en macOS, `cmd` en Windows), no dentro de otra herramienta que
bloquee internet.

### Registros, caché y modo depuración

Para ver el error técnico completo en pantalla, agregue `--debug` antes del comando (por ejemplo `pc --debug ocr`).

| | macOS | Windows |
|---|---|---|
| Registro de errores | `local_data/logs/pc.log` | `local_data\logs\pc.log` |
| Caché del OCR | `local_data/ocr_cache/<id del PDF>/` | `local_data\ocr_cache\<id del PDF>\` |
| Transcripciones para `ocr-bench` | `local_data/bench/` | `local_data\bench\` |

Cada archivo de la caché es una respuesta del modelo (`header`, `strip1of3`, `row15`…), útil para ver qué leyó.

## Para desarrolladores

```
uv sync
uv run pytest
uv run pc --help
```

Arquitectura, convenciones y reglas de datos: vea [AGENTS.md](AGENTS.md). Para repetir la instalación desde cero
(solo desarrollo): `scripts/dev-reset.sh`.
