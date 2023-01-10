// Make the DIV element draggable:

    function dragElement(elmnt) {
        var pos1 = 0, pos2 = 0, pos3 = 0, pos4 = 0;
        if ($(elmnt.id + "Header")) {
            // if present, the header is where you move the DIV from:
            $(elmnt.id + "Header").onmousedown = dragMouseDown;
        } else {
            // otherwise, move the DIV from anywhere inside the DIV:
            elmnt.onmousedown = dragMouseDown;
        }

        function dragMouseDown(e) {
            e = e || window.event;
            e.preventDefault();
            // get the mouse cursor position at startup:
            pos3 = e.clientX;
            pos4 = e.clientY;
            document.onmouseup = closeDragElement;
            // call a function whenever the cursor moves:
            document.onmousemove = elementDrag;
        }

        function elementDrag(e) {
            e = e || window.event;
            e.preventDefault();
            // calculate the new cursor position:
            pos1 = pos3 - e.clientX;
            pos2 = pos4 - e.clientY;
            pos3 = e.clientX;
            pos4 = e.clientY;
            // set the element's new position:
            elmnt.style.top = (elmnt.offsetTop - pos2) + "px";
            elmnt.style.left = (elmnt.offsetLeft - pos1) + "px";
        }

        function closeDragElement() {
            // stop moving when mouse button is released:
            document.onmouseup = null;
            document.onmousemove = null;
        }
    }

    function chatSend() {
        var formdata = new FormData();
        var message = $('chatInput').value.trim();

        if (!chatHaveAttachment && message == '') 
            return;

        if (chatHaveAttachment) {
            chatHaveAttachment = false;
            formdata.append('attachment', $('attachImage').files[0]);
            $('attachImage').value = null;
            $('chatInput').style.border = '1px solid black';
            console.log('image attached');
        }

        console.log(message);
        formdata.append('message', message);

        for (k of formdata.keys()) {
            console.log(k);
            console.log(formdata.get(k));
        }

        fetch(`/chat/${currGlider}${mission()}`,
        {
            method: 'POST',
            //headers: {
            //    'Content-Type': 'multipart/form-data'
            //},
            body: formdata, // JSON.stringify(json),
        })
        .then(res => res.text())
        .then(text => {
            if (text.includes("authorization failed")) {
                openLoginForm(function() { setupStream(currGlider, 0); chatSend(); } );
                return;
            }
            $('chatInput').value = '';
        })
        .catch(error => {
            alert(error);
        });

    }

    function chatMarkdown(currentGlider, input) {
        var link;
        var glider;
        var dive;
        var plot;
        const re = /(@(?<GDP_G>[0-9][0-9][0-9])#(?<GDP_D>[0-9]+)\$(?<GDP_P>[a-z0-9]+))|(@(?<GD_G>[0-9][0-9][0-9])#(?<GD_D>[0-9]+))|(@(?<G>[0-9][0-9][0-9]))|(#(?<D>[0-9]+))|(\$(?<P>[a-z0-9]+))|(#(?<DP_D>[0-9]+)\$(?<DP_P>[a-z0-9]+))|(?<link>\[(?<text>[\w\s\d]+)\]\((?<url>https?:\/\/([a-z0-9@#/.-]+))\))/g;
        let matches = input.matchAll(re);
        let out = '';
        let cursorPos = 0;

        console.log(input);
        console.log(matches);

        for (let match of matches) {
            console.log(match);
            const { groups: { GDP_G, GDP_D, GDP_P, GD_G, GD_D, G, D, P, DP_D, DP_P, link, text, url }, index } = match;
            const full = match[0];
            console.log(full);
 
            out += input.substr(cursorPos, (index - cursorPos));
            cursorPos = index + full.length;

            console.log(GD_G);
            console.log(GD_D);
            console.log(match.groups);
 
            if (GDP_G !== undefined) {
                glider = parseInt(GDP_G);
                dive   = parseInt(GDP_D);
                plot   = GDP_P;
                if (glider == currGlider) {
                    out += `<span class="spanClick" onclick="setDive(${dive}); jumpScrollByName('${plot}');">${match[0]}</a>`;
                }
                else { 
                    out += `<a href="/${glider}?dive=${dive}&plot=${plot}" target="${glider}">${match[0]}</a>`; 
                }
            }
            else if (GD_G !== undefined) {
                glider = parseInt(GD_G);
                dive   = parseInt(GD_D);
                if (glider == currGlider) {
                    out += `<span class="spanClick" onclick="setDive(${dive});">${match[0]}</a>`;
                }
                else { 
                    out += `<a href="/${glider}?dive=${dive}" target="${glider}">${full}</a>`; 
                }
            }
            else if (DP_D !== undefined) {
                dive = parseInt(DP_D);
                plot = DP_P;
                out += `<span class="spanClick" onclick="setDive(${dive}); jumpScrollByName('${plot}');">${match[0]}</a>`;
            }
            else if (P !== undefined) {
                out += `<span class="spanClick" onclick="jumpScrollByName('${P}');">${match[0]}</a>`;
            }
            else if (D !== undefined) {
                dive   = parseInt(D);
                out += `<span class="spanClick" onclick="setDive(${dive});">${match[0]}</a>`;
            }
            else if (G !== undefined) {
                glider = parseInt(G);
                if (glider != currentGlider) {
                    out += `<a href="/${glider}" target="${glider}">${full}</a>`; 
                }
            }
            else if (link !== undefined) {
                out += `<a href="${url}" target="_blank">${text}</a>`; 
            }
        }

        if (cursorPos < input.length) {
          out += input.substr(cursorPos, (input.length - cursorPos));
        }

        return out;
    }

    function chatClose() {
        $('chatDiv').style.display = 'none';
    }

    function chatShow() {
        $('chatDiv').style.display = 'block';
    }

    var chatHaveAttachment = false;
    function attachImageChange() {
        $('chatInput').style.border = "1px solid blue";
        chatHaveAttachment = true;
    }
