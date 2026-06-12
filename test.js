let page = document.getElementsByTagName("main")[0];
let footer = document.getElementsByTagName("footer")[0];
let aside = document.getElementsByTagName("aside")[0];
let button = document.getElementsByTagName("button")[0];
let spans = document.getElementsByTagName("span");

function openNav(){

    if (aside.style.width == "15rem") {
        aside.style.width = "0";
        spans[0].style.transform = "rotate(0deg)";
        spans[2].style.transform = "rotate(0deg)";
        spans[1].style.display = "block";
    } else {
        aside.style.width = "15rem";
        spans[1].style.display = "none";
        spans[0].style.transform = "translateY(0.425rem) rotate(-45deg)";
        spans[2].style.transform = "translateY(-0.425rem) rotate(45deg)";
    }

    page.classList.toggle('darkened');
    footer.classList.toggle('darkened');
}

button.addEventListener("click", openNav);
