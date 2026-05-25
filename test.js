function openNav(){
    let aside = document.getElementsByTagName("aside")[0]
    let spans = document.getElementsByTagName("span")
    if(aside.style.width === "10%"){
        document.getElementsByTagName("aside")[0].style.width = "0px";
        document.body.style.backgroundColor = "#fff";
        spans[0].style.backgroundColor = "black";
        spans[2].style.backgroundColor = "black";
        spans[0].style.transform = "rotate(0deg)";
        spans[2].style.transform = "rotate(0deg)";
        spans[1].style.display = "block";
    }

    else{
        document.getElementsByTagName("aside")[0].style.width = "10%";
        document.body.style.backgroundColor = "rgba(0,0,0,0.4)";
        spans[0].style.backgroundColor = "white";
        spans[1].style.display = "none";
        spans[2].style.backgroundColor = "white";
        spans[0].style.transform = "translateY(4px) rotate(-45deg)";
        spans[2].style.transform = "translateY(-4px) rotate(45deg)";
    }
    
}

let button = document.getElementsByTagName("button");
button[0].addEventListener("click", openNav);